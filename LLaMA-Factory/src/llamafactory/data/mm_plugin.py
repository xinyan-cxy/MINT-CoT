import inspect
import math
import re
from copy import deepcopy
from io import BytesIO
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence, Tuple, TypedDict, Union

import numpy as np
import torch
from transformers.image_utils import get_image_size, to_numpy_array
from typing_extensions import override

from ..extras.constants import AUDIO_PLACEHOLDER, IGNORE_INDEX, IMAGE_PLACEHOLDER, VIDEO_PLACEHOLDER, INTERLEAVE_PLACEHOLDER
from ..extras.packages import (
    is_librosa_available,
    is_pillow_available,
    is_pyav_available,
    is_transformers_version_greater_than,
)


if is_librosa_available():
    import librosa


if is_pillow_available():
    from PIL import Image
    from PIL.Image import Image as ImageObject


if is_pyav_available():
    import av


if is_transformers_version_greater_than("4.45.0"):
    from transformers.models.mllama.processing_mllama import (
        convert_sparse_cross_attention_mask_to_dense,
        get_cross_attention_token_mask,
    )


if TYPE_CHECKING:
    from av.stream import Stream
    from numpy.typing import NDArray
    from transformers import PreTrainedTokenizer, ProcessorMixin
    from transformers.feature_extraction_sequence_utils import SequenceFeatureExtractor
    from transformers.image_processing_utils import BaseImageProcessor

    class EncodedImage(TypedDict):
        path: Optional[str]
        bytes: Optional[bytes]

    ImageInput = Union[str, bytes, EncodedImage, ImageObject]
    VideoInput = str
    AudioInput = Union[str, NDArray]


def _get_paligemma_token_type_ids(
    imglens: Sequence[int], seqlens: Sequence[int], processor: "ProcessorMixin"
) -> List[List[int]]:
    r"""
    Gets paligemma token type ids for computing loss.

    Returns:
        batch_token_type_ids: shape (batch_size, sequence_length)
    """
    batch_token_type_ids = []
    for imglen, seqlen in zip(imglens, seqlens):
        image_seqlen = imglen * getattr(processor, "image_seqlen")
        batch_token_type_ids.append([0] * image_seqlen + [1] * (seqlen - image_seqlen))

    return batch_token_type_ids


class BasePlugin:
    def __init__(self, image_token: Optional[str], video_token: Optional[str], audio_token: Optional[str]) -> None:
        self.image_token = image_token
        self.video_token = video_token
        self.audio_token = audio_token
        self.expand_mm_tokens = True

    def _validate_input(
        self,
        images: Sequence["ImageInput"],
        videos: Sequence["VideoInput"],
        audios: Sequence["AudioInput"],
    ) -> None:
        r"""
        Validates if this model accepts the input modalities.
        """
        if len(images) != 0 and self.image_token is None:
            raise ValueError(
                "This model does not support image input. Please check whether the correct `template` is used."
            )

        if len(videos) != 0 and self.video_token is None:
            raise ValueError(
                "This model does not support video input. Please check whether the correct `template` is used."
            )

        if len(audios) != 0 and self.audio_token is None:
            raise ValueError(
                "This model does not support audio input. Please check whether the correct `template` is used."
            )

    def _preprocess_image(self, image: "ImageObject", **kwargs) -> "ImageObject":
        r"""
        Pre-processes a single image.
        """
        image_resolution: int = kwargs["image_resolution"]
        if (image.width * image.height) > image_resolution:
            resize_factor = math.sqrt(image_resolution / (image.width * image.height))
            width, height = int(image.width * resize_factor), int(image.height * resize_factor)
            image = image.resize((width, height), resample=Image.Resampling.NEAREST)

        if image.mode != "RGB":
            image = image.convert("RGB")

        return image

    def _get_video_sample_indices(self, video_stream: "Stream", **kwargs) -> List[int]:
        r"""
        Computes video sample indices according to fps.
        """
        video_fps: float = kwargs["video_fps"]
        video_maxlen: int = kwargs["video_maxlen"]
        total_frames = video_stream.frames
        if total_frames == 0:  # infinite video
            return np.linspace(0, video_maxlen - 1, video_maxlen).astype(np.int32)

        sample_frames = math.floor(float(video_stream.duration * video_stream.time_base) * video_fps)
        sample_frames = min(total_frames, video_maxlen, sample_frames)
        return np.linspace(0, total_frames - 1, sample_frames).astype(np.int32)

    def _regularize_images(self, images: Sequence["ImageInput"], **kwargs) -> List["ImageObject"]:
        r"""
        Regularizes images to avoid error. Including reading and pre-processing.
        """
        results = []
        for image in images:
            if isinstance(image, str):
                image = Image.open(image)
            elif isinstance(image, bytes):
                image = Image.open(BytesIO(image))
            elif isinstance(image, dict):
                if image["bytes"] is not None:
                    image = Image.open(BytesIO(image["bytes"]))
                else:
                    image = Image.open(image["path"])

            if not isinstance(image, ImageObject):
                raise ValueError(f"Expect input is a list of images, but got {type(image)}.")

            results.append(self._preprocess_image(image, **kwargs))

        return results

    def _regularize_videos(self, videos: Sequence["VideoInput"], **kwargs) -> List[List["ImageObject"]]:
        r"""
        Regularizes videos to avoid error. Including reading, resizing and converting.
        """
        results = []
        for video in videos:
            container = av.open(video, "r")
            video_stream = next(stream for stream in container.streams if stream.type == "video")
            sample_indices = self._get_video_sample_indices(video_stream, **kwargs)
            frames: List["ImageObject"] = []
            container.seek(0)
            for frame_idx, frame in enumerate(container.decode(video_stream)):
                if frame_idx in sample_indices:
                    frames.append(frame.to_image())

            frames = self._regularize_images(frames, **kwargs)
            results.append(frames)

        return results

    def _regularize_audios(self, audios: Sequence["AudioInput"], **kwargs) -> List["NDArray"]:
        r"""
        Regularizes audios to avoid error. Including reading and resampling.
        """
        results = []
        sampling_rate = kwargs["sampling_rate"]
        for audio in audios:
            if isinstance(audio, str):
                audio = librosa.load(audio, sr=sampling_rate)[0]

            if not isinstance(audio, np.ndarray):
                raise ValueError(f"Expect input is a list of audios, but got {type(audio)}.")

            results.append(audio)

        return results

    def _get_mm_inputs(
        self,
        images: Sequence["ImageInput"],
        videos: Sequence["VideoInput"],
        audios: Sequence["AudioInput"],
        processor: "ProcessorMixin",
    ) -> Dict[str, "torch.Tensor"]:
        r"""
        Processes visual inputs.

        Returns: (llava and paligemma)
            pixel_values: tensor with shape (B, C, H, W)

        Returns: (qwen2-vl)
            pixel_values: tensor with shape (num_patches, patch_dim)
            image_grid_thw: tensor with shape (num_images, 3), where the three numbers are time, width, height

        It holds num_patches == torch.prod(image_grid_thw)
        """
        image_processor: "BaseImageProcessor" = getattr(processor, "image_processor", None)
        video_processor: "BaseImageProcessor" = getattr(processor, "video_processor", image_processor)
        feature_extractor: "SequenceFeatureExtractor" = getattr(processor, "feature_extractor", None)
        mm_inputs = {}

        if len(images) != 0:
            images = self._regularize_images(
                images,
                image_resolution=getattr(processor, "image_resolution", 768 * 768),
            )
            mm_inputs.update(image_processor(images, return_tensors="pt"))

        if len(videos) != 0:
            videos = self._regularize_videos(
                videos,
                image_resolution=getattr(processor, "video_resolution", 256 * 256),
                video_fps=getattr(processor, "video_fps", 2.0),
                video_maxlen=getattr(processor, "video_maxlen", 128),
            )
            if "videos" in inspect.signature(video_processor.preprocess).parameters:  # qwen2vl processor
                mm_inputs.update(video_processor(images=None, videos=videos, return_tensors="pt"))
            else:
                mm_inputs.update(video_processor(videos, return_tensors="pt"))

        if len(audios) != 0:
            audios = self._regularize_audios(
                audios,
                sampling_rate=getattr(feature_extractor, "sampling_rate", 16000),
            )
            mm_inputs.update(
                feature_extractor(
                    audios,
                    sampling_rate=getattr(feature_extractor, "sampling_rate", 16000),
                    return_attention_mask=True,
                    padding="max_length",
                    return_tensors="pt",
                )
            )
            mm_inputs["feature_attention_mask"] = mm_inputs.pop("attention_mask")  # prevent conflicts

        return mm_inputs

    def process_messages(
        self,
        messages: Sequence[Dict[str, str]],
        images: Sequence["ImageInput"],
        videos: Sequence["VideoInput"],
        audios: Sequence["AudioInput"],
        processor: Optional["ProcessorMixin"],
    ) -> List[Dict[str, str]]:
        r"""
        Pre-processes input messages before tokenization for VLMs.
        """
        self._validate_input(images, videos, audios)
        return messages

    def process_token_ids(
        self,
        input_ids: List[int],
        labels: Optional[List[int]],
        images: Sequence["ImageInput"],
        videos: Sequence["VideoInput"],
        audios: Sequence["AudioInput"],
        tokenizer: "PreTrainedTokenizer",
        processor: Optional["ProcessorMixin"],
    ) -> Tuple[List[int], Optional[List[int]]]:
        r"""
        Pre-processes token ids after tokenization for VLMs.
        """
        self._validate_input(images, videos, audios)
        return input_ids, labels

    def get_mm_inputs(
        self,
        images: Sequence["ImageInput"],
        videos: Sequence["VideoInput"],
        audios: Sequence["AudioInput"],
        imglens: Sequence[int],
        vidlens: Sequence[int],
        audlens: Sequence[int],
        batch_ids: Sequence[List[int]],
        processor: Optional["ProcessorMixin"],
    ) -> Dict[str, Union[List[int], "torch.Tensor"]]:
        r"""
        Builds batched multimodal inputs for VLMs.

        Arguments:
            images: a list of image inputs, shape (num_images,)
            videos: a list of video inputs, shape (num_videos,)
            imglens: number of images in each sample, shape (batch_size,)
            vidlens: number of videos in each sample, shape (batch_size,)
            audlens: number of audios in each sample, shape (batch_size,)
            batch_ids: token ids of input samples, shape (batch_size, seq_len)
            processor: a processor for pre-processing images and videos
        """
        self._validate_input(images, videos, audios)
        return {}


class LlavaPlugin(BasePlugin):
    @override
    def process_messages(
        self,
        messages: Sequence[Dict[str, str]],
        images: Sequence["ImageInput"],
        videos: Sequence["VideoInput"],
        audios: Sequence["AudioInput"],
        processor: Optional["ProcessorMixin"],
    ) -> List[Dict[str, str]]:
        self._validate_input(images, videos, audios)
        num_image_tokens = 0
        image_seqlen = getattr(processor, "image_seqlen") if self.expand_mm_tokens else 1
        messages = deepcopy(messages)
        for message in messages:
            content = message["content"]
            while IMAGE_PLACEHOLDER in content:
                content = content.replace(IMAGE_PLACEHOLDER, "{{image}}" * image_seqlen, 1)
                num_image_tokens += 1

            message["content"] = content.replace("{{image}}", self.image_token)

        if len(images) != num_image_tokens:
            raise ValueError(f"The number of images does not match the number of {IMAGE_PLACEHOLDER} tokens.")

        return messages

    @override
    def get_mm_inputs(
        self,
        images: Sequence["ImageInput"],
        videos: Sequence["VideoInput"],
        audios: Sequence["AudioInput"],
        imglens: Sequence[int],
        vidlens: Sequence[int],
        audlens: Sequence[int],
        batch_ids: Sequence[List[int]],
        processor: Optional["ProcessorMixin"],
    ) -> Dict[str, Union[List[int], "torch.Tensor"]]:
        self._validate_input(images, videos, audios)
        return self._get_mm_inputs(images, videos, audios, processor)


class LlavaNextPlugin(BasePlugin):
    @override
    def process_messages(
        self,
        messages: Sequence[Dict[str, str]],
        images: Sequence["ImageInput"],
        videos: Sequence["VideoInput"],
        audios: Sequence["AudioInput"],
        processor: Optional["ProcessorMixin"],
    ) -> List[Dict[str, str]]:
        self._validate_input(images, videos, audios)
        num_image_tokens = 0
        messages = deepcopy(messages)
        mm_inputs = self._get_mm_inputs(images, videos, audios, processor)
        if "image_sizes" in mm_inputs:
            image_sizes = iter(mm_inputs["image_sizes"])

        if "pixel_values" in mm_inputs:
            height, width = get_image_size(to_numpy_array(mm_inputs["pixel_values"][0][0]))

        for message in messages:
            content = message["content"]
            while IMAGE_PLACEHOLDER in content:
                if self.expand_mm_tokens:
                    orig_height, orig_width = next(image_sizes)
                    image_seqlen = processor._get_number_of_features(orig_height, orig_width, height, width)
                    if getattr(processor, "vision_feature_select_strategy", "default") == "default":
                        image_seqlen -= 1
                else:
                    image_seqlen = 1

                content = content.replace(IMAGE_PLACEHOLDER, "{{image}}" * image_seqlen, 1)
                num_image_tokens += 1

            message["content"] = content.replace("{{image}}", self.image_token)

        if len(images) != num_image_tokens:
            raise ValueError(f"The number of images does not match the number of {IMAGE_PLACEHOLDER} tokens.")

        return messages

    @override
    def get_mm_inputs(
        self,
        images: Sequence["ImageInput"],
        videos: Sequence["VideoInput"],
        audios: Sequence["AudioInput"],
        imglens: Sequence[int],
        vidlens: Sequence[int],
        audlens: Sequence[int],
        batch_ids: Sequence[List[int]],
        processor: Optional["ProcessorMixin"],
    ) -> Dict[str, Union[List[int], "torch.Tensor"]]:
        self._validate_input(images, videos, audios)
        return self._get_mm_inputs(images, videos, audios, processor)


class LlavaNextVideoPlugin(BasePlugin):
    @override
    def process_messages(
        self,
        messages: Sequence[Dict[str, str]],
        images: Sequence["ImageInput"],
        videos: Sequence["VideoInput"],
        audios: Sequence["AudioInput"],
        processor: Optional["ProcessorMixin"],
    ) -> List[Dict[str, str]]:
        self._validate_input(images, videos, audios)
        num_image_tokens, num_video_tokens = 0, 0
        messages = deepcopy(messages)
        mm_inputs = self._get_mm_inputs(images, videos, audios, processor)
        if "pixel_values" in mm_inputs:
            image_sizes = iter(mm_inputs["image_sizes"])
            height, width = get_image_size(to_numpy_array(mm_inputs["pixel_values"][0][0]))
            for message in messages:
                content = message["content"]
                while IMAGE_PLACEHOLDER in content:
                    if self.expand_mm_tokens:
                        orig_height, orig_width = next(image_sizes)
                        image_seqlen = processor._get_number_of_features(orig_height, orig_width, height, width)
                        if getattr(processor, "vision_feature_select_strategy", "default") == "default":
                            image_seqlen -= 1
                    else:
                        image_seqlen = 1

                    content = content.replace(IMAGE_PLACEHOLDER, "{{image}}" * image_seqlen, 1)
                    num_image_tokens += 1

                message["content"] = content.replace("{{image}}", self.image_token)

        if "pixel_values_videos" in mm_inputs:
            pixel_values_video = to_numpy_array(mm_inputs.get("pixel_values_videos")[0])
            height, width = get_image_size(pixel_values_video[0])
            num_frames = pixel_values_video.shape[0]  # frame dim is always after batch dim
            image_seqlen = (height // processor.patch_size) * (width // processor.patch_size)
            video_seqlen = image_seqlen // 4 * num_frames  # divide by 4 needed for avg pooling layer
            video_seqlen = video_seqlen if self.expand_mm_tokens else 1
            for message in messages:
                content = message["content"]
                while VIDEO_PLACEHOLDER in content:
                    num_video_tokens += 1
                    content = content.replace(VIDEO_PLACEHOLDER, "{{video}}" * video_seqlen, 1)

                message["content"] = content.replace("{{video}}", self.video_token)

        if len(images) != num_image_tokens:
            raise ValueError(f"The number of images does not match the number of {IMAGE_PLACEHOLDER} tokens.")

        if len(videos) != num_video_tokens:
            raise ValueError(f"The number of videos does not match the number of {VIDEO_PLACEHOLDER} tokens.")

        return messages

    @override
    def get_mm_inputs(
        self,
        images: Sequence["ImageInput"],
        videos: Sequence["VideoInput"],
        audios: Sequence["AudioInput"],
        imglens: Sequence[int],
        vidlens: Sequence[int],
        audlens: Sequence[int],
        batch_ids: Sequence[List[int]],
        processor: Optional["ProcessorMixin"],
    ) -> Dict[str, Union[List[int], "torch.Tensor"]]:
        self._validate_input(images, videos, audios)
        return self._get_mm_inputs(images, videos, audios, processor)


class MiniCPMVPlugin(BasePlugin):
    @override
    def process_messages(
        self,
        messages: Sequence[Dict[str, str]],
        images: Sequence["ImageInput"],
        videos: Sequence["VideoInput"],
        audios: Sequence["AudioInput"],
        processor: Optional["ProcessorMixin"],
    ) -> List[Dict[str, str]]:
        self._validate_input(images, videos, audios)
        num_image_tokens = 0
        num_video_tokens = 0
        num_audio_tokens = 0
        messages = deepcopy(messages)
        image_processor: "BaseImageProcessor" = getattr(processor, "image_processor")
        mm_inputs = {}
        audio_inputs = {}
        audio_parts = []
        if len(images) != 0 and len(videos) != 0:
            raise ValueError("MiniCPM-V model does not support input images and videos at the same time.")

        if len(videos) != 0:
            max_slice_nums = 2
            use_image_id = False
            mm_inputs = self._get_mm_inputs([], videos, [], processor)
        else:
            max_slice_nums = image_processor.max_slice_nums
            use_image_id = image_processor.use_image_id

        for i, message in enumerate(messages):
            content = message["content"]
            while IMAGE_PLACEHOLDER in content:
                content = content.replace(IMAGE_PLACEHOLDER, "{{image}}", 1)
                num_image_tokens += 1

            while VIDEO_PLACEHOLDER in content:
                video_seqlen = len(mm_inputs["pixel_values"][num_video_tokens]) if self.expand_mm_tokens else 1
                content = content.replace(VIDEO_PLACEHOLDER, "{{image}}" * video_seqlen, 1)
                num_video_tokens += 1

            while AUDIO_PLACEHOLDER in content:
                audio_parts.append(i)
                content = content.replace(AUDIO_PLACEHOLDER, "{{audio}}", 1)
                num_audio_tokens += 1

            message["content"] = content.replace("{{image}}", "(<image>./</image>)").replace(
                "{{audio}}", "(<audio>./</audio>)"
            )

        if num_image_tokens > 0:
            mm_inputs = self._get_mm_inputs(images, [], [], processor)

        if num_audio_tokens > 0:
            audio_parts_ls = [audio_parts]
            audio_inputs = self._get_mm_inputs([], [], audios, processor, audio_parts_ls=audio_parts_ls, ret_phs=True)

        if mm_inputs:
            pattern = "(<image>./</image>)"
            image_sizes = mm_inputs["image_sizes"]
            for index, message in enumerate(messages):
                text = message["content"]
                image_tags = re.findall(pattern, text)
                text_chunks = text.split(pattern)
                final_text = ""
                for i in range(len(image_tags)):
                    final_text = (
                        final_text
                        + text_chunks[i]
                        + image_processor.get_slice_image_placeholder(
                            image_sizes[0][i], i, max_slice_nums, use_image_id
                        )
                    )

                final_text += text_chunks[-1]
                messages[index]["content"] = final_text

        if audio_inputs:
            pattern = "(<audio>./</audio>)"
            for index, message in enumerate(messages):
                text = message["content"]
                audio_tags = re.findall(pattern, text)
                text_chunks = text.split(pattern)
                final_text = ""
                for i in range(len(audio_tags)):
                    audio_placeholder = audio_inputs["audio_phs"][0][i]
                    final_text = final_text + text_chunks[i] + audio_placeholder

                final_text += text_chunks[-1]
                messages[index]["content"] = final_text

        if len(images) != num_image_tokens:
            raise ValueError(f"The number of images does not match the number of {IMAGE_PLACEHOLDER} tokens.")

        if len(videos) != num_video_tokens:
            raise ValueError(f"The number of videos does not match the number of {VIDEO_PLACEHOLDER} tokens.")

        if len(audios) != num_audio_tokens:
            raise ValueError(f"The number of audios does not match the number of {AUDIO_PLACEHOLDER} tokens.")

        return messages

    @override
    def _get_mm_inputs(
        self,
        images: Sequence["ImageInput"],
        videos: Sequence["VideoInput"],
        audios: Sequence["AudioInput"],
        processor: "ProcessorMixin",
        **kwargs,
    ) -> Dict[str, "torch.Tensor"]:
        image_processor: "BaseImageProcessor" = getattr(processor, "image_processor")
        mm_inputs = {}
        if len(images) != 0:
            images = self._regularize_images(
                images,
                image_resolution=getattr(processor, "image_resolution", 768 * 768),
            )
            if "valid_image_nums_ls" in kwargs:
                valid_image_nums_ls = kwargs["valid_image_nums_ls"]
                new_images = []
                idx = 0
                for valid_image_nums in valid_image_nums_ls:
                    new_images.append(images[idx : idx + valid_image_nums])
                    idx += valid_image_nums

                images = new_images

            image_inputs = image_processor(
                images, do_pad=True, max_slice_nums=image_processor.max_slice_nums, return_tensors="pt"
            )
            mm_inputs.update(image_inputs)

        if len(videos) != 0:
            videos = self._regularize_videos(
                videos,
                image_resolution=getattr(processor, "video_resolution", 256 * 256),
                video_fps=getattr(processor, "video_fps", 2.0),
                video_maxlen=getattr(processor, "video_maxlen", 128),
            )
            video_inputs = image_processor(videos, do_pad=True, max_slice_nums=2, return_tensors="pt")
            mm_inputs.update(video_inputs)

        if len(audios) != 0:
            audio_parts_ls = kwargs.get("audio_parts_ls", None)
            new_audios = []
            for audio in audios:
                if not isinstance(audio, np.ndarray):
                    audio = librosa.load(audio, sr=processor.feature_extractor.sampling_rate)[0]
                new_audios.append(audio)

            audios_ls = []
            idx = 0
            for audio_parts in audio_parts_ls:
                audios_ls.append(new_audios[idx : idx + len(audio_parts)])
                idx += len(audio_parts)

            audio_features, audio_feature_lens, audio_phs = processor.audio_feature_extract(
                audios_ls,
                audio_parts_ls,
                chunk_input=True,
                sampling_rate=16000,
            )
            mm_inputs.update({"audio_features": audio_features, "audio_feature_lens": audio_feature_lens})
            if kwargs.get("ret_phs", False):
                mm_inputs.update({"audio_phs": audio_phs})

        return mm_inputs

    @override
    def get_mm_inputs(
        self,
        images: Sequence["ImageInput"],
        videos: Sequence["VideoInput"],
        audios: Sequence["AudioInput"],
        imglens: Sequence[int],
        vidlens: Sequence[int],
        audlens: Sequence[int],
        batch_ids: Sequence[List[int]],
        processor: Optional["ProcessorMixin"],
    ) -> Dict[str, Union[List[int], "torch.Tensor"]]:
        self._validate_input(images, videos, audios)

        # image bound
        image_bounds_list = []
        valid_image_nums_ls = []
        for i, input_ids in enumerate(batch_ids):
            input_ids_ = torch.tensor(input_ids)
            start_cond = (input_ids_ == processor.tokenizer.im_start_id) | (
                input_ids_ == processor.tokenizer.slice_start_id
            )
            end_cond = (input_ids_ == processor.tokenizer.im_end_id) | (input_ids_ == processor.tokenizer.slice_end_id)
            image_start_tokens = torch.where(start_cond)[0]
            image_start_tokens += 1
            image_end_tokens = torch.where(end_cond)[0]
            valid_image_nums_ls.append(imglens[i])
            image_bounds = torch.hstack(
                [
                    image_start_tokens.unsqueeze(-1),
                    image_end_tokens.unsqueeze(-1),
                ]
            )
            image_bounds_list.append(image_bounds)

        mm_inputs = self._get_mm_inputs(images, videos, [], processor, valid_image_nums_ls=valid_image_nums_ls)
        if "tgt_sizes" not in mm_inputs:
            dummy_data = [torch.empty(0) for _ in range(len(batch_ids))]
            mm_inputs.update({"tgt_sizes": dummy_data, "pixel_values": dummy_data, "image_sizes": dummy_data})

        mm_inputs.update({"image_bound": image_bounds_list})

        if len(audios) > 0:
            # audio bound
            audio_bounds_ls = []
            spk_bounds_ls = []
            audio_parts_ls = []

            for input_ids, audiolen in zip(batch_ids, audlens):
                input_ids_ = torch.tensor(input_ids)
                audio_start_idx = torch.where(input_ids_ == processor.tokenizer.audio_start_id)[0]
                audio_end_idx = torch.where(input_ids_ == processor.tokenizer.audio_end_id)[0]
                assert len(audio_start_idx) == len(audio_end_idx)
                audio_bounds = torch.hstack([(audio_start_idx + 1).unsqueeze(-1), audio_end_idx.unsqueeze(-1)])
                audio_bounds_ls.append(audio_bounds)
                audio_parts_ls.append(list(range(audiolen)))

                spk_start_idx = torch.where(input_ids_ == processor.tokenizer.spk_start_id)[0]
                spk_end_idx = torch.where(input_ids_ == processor.tokenizer.spk_end_id)[0]
                assert len(spk_start_idx) == len(spk_end_idx)
                spk_bounds = torch.hstack([(spk_start_idx + 1).unsqueeze(-1), spk_end_idx.unsqueeze(-1)])
                spk_bounds_ls.append(spk_bounds)

            audio_inputs = self._get_mm_inputs([], [], audios, processor, audio_parts_ls=audio_parts_ls)
            mm_inputs.update(audio_inputs)
            mm_inputs.update({"audio_bounds": audio_bounds_ls, "spk_bounds": spk_bounds_ls})

        return mm_inputs


class MllamaPlugin(BasePlugin):
    @override
    def process_messages(
        self,
        messages: Sequence[Dict[str, str]],
        images: Sequence["ImageInput"],
        videos: Sequence["VideoInput"],
        audios: Sequence["AudioInput"],
        processor: Optional["ProcessorMixin"],
    ) -> List[Dict[str, str]]:
        self._validate_input(images, videos, audios)
        num_image_tokens = 0
        messages = deepcopy(messages)
        for message in messages:
            content = message["content"]
            num_image_tokens += content.count(IMAGE_PLACEHOLDER)
            message["content"] = content.replace(IMAGE_PLACEHOLDER, self.image_token)

        if len(images) != num_image_tokens:
            raise ValueError(f"The number of images does not match the number of {IMAGE_PLACEHOLDER} tokens.")

        return messages

    @override
    def _get_mm_inputs(
        self,
        images: Sequence["ImageInput"],
        videos: Sequence["VideoInput"],
        audios: Sequence["AudioInput"],
        processor: "ProcessorMixin",
        **kwargs,
    ) -> Dict[str, "torch.Tensor"]:
        r"""
        Processes visual inputs for mllama because its image processor only accepts List[List[ImageInput]].

        Returns:
            pixel_values: tensor with shape
                          (batch_size, max_num_images, max_image_tiles, channels, tile_height, tile_width)
                          For example, (2, 1, 4, 3, 560, 560).
            aspect_ratio_ids: tensor with shape (batch_size, max_num_images). For example, (2, 1).
            aspect_ratio_mask: tensor with shape (batch_size, max_num_images, max_image_tiles). For example, (2, 1, 4).
            num_tiles: List[List[int]] with shape (batch_size, num_images_in_batch). For example, (2, 1).
        """
        image_processor: "BaseImageProcessor" = getattr(processor, "image_processor")
        imglens: List[int] = kwargs["imglens"]
        images = self._regularize_images(images, image_resolution=getattr(processor, "image_resolution", 768 * 768))
        batch_images = []
        for image_length in imglens:
            batch_images.append(images[:image_length])
            images = images[image_length:]

        return image_processor(batch_images, return_tensors="pt")

    @override
    def get_mm_inputs(
        self,
        images: Sequence["ImageInput"],
        videos: Sequence["VideoInput"],
        audios: Sequence["AudioInput"],
        imglens: Sequence[int],
        vidlens: Sequence[int],
        audlens: Sequence[int],
        batch_ids: Sequence[List[int]],
        processor: Optional["ProcessorMixin"],
    ) -> Dict[str, Union[List[int], "torch.Tensor"]]:
        self._validate_input(images, videos, audios)
        mm_inputs = self._get_mm_inputs(images, videos, audios, processor, imglens=imglens)
        num_tiles = mm_inputs.pop("num_tiles")
        image_token_id = getattr(processor, "image_token_id")
        max_image_tiles = getattr(processor.image_processor, "max_image_tiles")
        cross_attention_token_mask = [
            get_cross_attention_token_mask(input_ids, image_token_id) for input_ids in batch_ids
        ]
        mm_inputs["cross_attention_mask"] = torch.from_numpy(
            convert_sparse_cross_attention_mask_to_dense(
                cross_attention_token_mask,
                num_tiles=num_tiles,
                max_num_tiles=max_image_tiles,
                length=max(len(input_ids) for input_ids in batch_ids),
            )
        )  # shape: (batch_size, length, max_num_images, max_num_tiles)
        return mm_inputs


class PaliGemmaPlugin(BasePlugin):
    @override
    def process_messages(
        self,
        messages: Sequence[Dict[str, str]],
        images: Sequence["ImageInput"],
        videos: Sequence["VideoInput"],
        audios: Sequence["AudioInput"],
        processor: Optional["ProcessorMixin"],
    ) -> List[Dict[str, str]]:
        self._validate_input(images, videos, audios)
        num_image_tokens = 0
        messages = deepcopy(messages)
        for message in messages:
            content = message["content"]
            while IMAGE_PLACEHOLDER in content:
                content = content.replace(IMAGE_PLACEHOLDER, "{{image}}", 1)
                num_image_tokens += 1

            message["content"] = content.replace("{{image}}", "")

        if len(images) != num_image_tokens:
            raise ValueError(f"The number of images does not match the number of {IMAGE_PLACEHOLDER} tokens.")

        return messages

    @override
    def process_token_ids(
        self,
        input_ids: List[int],
        labels: Optional[List[int]],
        images: Sequence["ImageInput"],
        videos: Sequence["VideoInput"],
        audios: Sequence["AudioInput"],
        tokenizer: "PreTrainedTokenizer",
        processor: Optional["ProcessorMixin"],
    ) -> Tuple[List[int], Optional[List[int]]]:
        self._validate_input(images, videos, audios)
        num_images = len(images)
        image_seqlen = num_images * getattr(processor, "image_seqlen") if self.expand_mm_tokens else 0  # skip mm token
        image_token_id = tokenizer.convert_tokens_to_ids(self.image_token)
        input_ids = [image_token_id] * image_seqlen + input_ids
        if labels is not None:
            labels = [IGNORE_INDEX] * image_seqlen + labels

        return input_ids, labels

    @override
    def get_mm_inputs(
        self,
        images: Sequence["ImageInput"],
        videos: Sequence["VideoInput"],
        audios: Sequence["AudioInput"],
        imglens: Sequence[int],
        vidlens: Sequence[int],
        audlens: Sequence[int],
        batch_ids: Sequence[List[int]],
        processor: Optional["ProcessorMixin"],
    ) -> Dict[str, Union[List[int], "torch.Tensor"]]:
        self._validate_input(images, videos, audios)
        seqlens = [len(input_ids) for input_ids in batch_ids]
        mm_inputs = self._get_mm_inputs(images, videos, audios, processor)
        mm_inputs["token_type_ids"] = _get_paligemma_token_type_ids(imglens, seqlens, processor)
        return mm_inputs


class PixtralPlugin(BasePlugin):
    @override
    def process_messages(
        self,
        messages: Sequence[Dict[str, str]],
        images: Sequence["ImageInput"],
        videos: Sequence["VideoInput"],
        audios: Sequence["AudioInput"],
        processor: Optional["ProcessorMixin"],
    ) -> List[Dict[str, str]]:
        self._validate_input(images, videos, audios)
        patch_size = getattr(processor, "patch_size")
        image_token = getattr(processor, "image_token")
        image_break_token = getattr(processor, "image_break_token")
        image_end_token = getattr(processor, "image_end_token")

        num_image_tokens = 0
        messages = deepcopy(messages)
        mm_inputs = self._get_mm_inputs(images, videos, audios, processor)
        image_input_sizes = mm_inputs.get("image_sizes", None)
        for message in messages:
            content = message["content"]
            while IMAGE_PLACEHOLDER in content:
                if image_input_sizes is None:
                    raise ValueError("Cannot get image input sizes.")

                if self.expand_mm_tokens:
                    image_size = image_input_sizes[0][num_image_tokens]
                    height, width = image_size
                    num_height_tokens = height // patch_size
                    num_width_tokens = width // patch_size
                    replace_tokens = [[image_token] * num_width_tokens + [image_break_token]] * num_height_tokens
                    replace_tokens = [item for sublist in replace_tokens for item in sublist]  # flatten list
                    replace_tokens[-1] = image_end_token
                    replace_str = "".join(replace_tokens)
                else:
                    replace_str = image_token

                content = content.replace(IMAGE_PLACEHOLDER, replace_str, 1)
                num_image_tokens += 1

            message["content"] = content

        if len(images) != num_image_tokens:
            raise ValueError(f"The number of images does not match the number of {IMAGE_PLACEHOLDER} tokens.")

        return messages

    @override
    def get_mm_inputs(
        self,
        images: Sequence["ImageInput"],
        videos: Sequence["VideoInput"],
        audios: Sequence["AudioInput"],
        imglens: Sequence[int],
        vidlens: Sequence[int],
        audlens: Sequence[int],
        batch_ids: Sequence[List[int]],
        processor: Optional["ProcessorMixin"],
    ) -> Dict[str, Union[List[int], "torch.Tensor"]]:
        self._validate_input(images, videos, audios)
        mm_inputs = self._get_mm_inputs(images, videos, audios, processor)
        if mm_inputs.get("pixel_values"):
            mm_inputs["pixel_values"] = mm_inputs["pixel_values"][0]

        mm_inputs.pop("image_sizes", None)
        return mm_inputs


class Qwen2AudioPlugin(BasePlugin):
    @override
    def process_messages(
        self,
        messages: Sequence[Dict[str, str]],
        images: Sequence["ImageInput"],
        videos: Sequence["VideoInput"],
        audios: Sequence["AudioInput"],
        processor: Optional["ProcessorMixin"],
    ) -> List[Dict[str, str]]:
        self._validate_input(images, videos, audios)
        bos_token: str = getattr(processor, "audio_bos_token")
        eos_token: str = getattr(processor, "audio_eos_token")
        mm_inputs = self._get_mm_inputs([], [], audios, processor)
        if "feature_attention_mask" in mm_inputs:
            audio_lengths = mm_inputs["feature_attention_mask"].sum(-1).tolist()

        num_audio_tokens = 0
        for message in messages:
            content = message["content"]
            while AUDIO_PLACEHOLDER in content:
                audio_length = audio_lengths.pop(0)
                input_length = (audio_length - 1) // 2 + 1
                audio_seqlen = (input_length - 2) // 2 + 1 if self.expand_mm_tokens else 1
                content = content.replace(
                    AUDIO_PLACEHOLDER, f"{bos_token}{self.audio_token * audio_seqlen}{eos_token}", 1
                )
                num_audio_tokens += 1

            message["content"] = content

        if len(audios) != num_audio_tokens:
            raise ValueError(f"The number of audios does not match the number of {AUDIO_PLACEHOLDER} tokens.")

        return messages

    @override
    def get_mm_inputs(
        self,
        images: Sequence["ImageInput"],
        videos: Sequence["VideoInput"],
        audios: Sequence["AudioInput"],
        imglens: Sequence[int],
        vidlens: Sequence[int],
        audlens: Sequence[int],
        batch_ids: Sequence[List[int]],
        processor: Optional["ProcessorMixin"],
    ) -> Dict[str, Union[List[int], "torch.Tensor"]]:
        self._validate_input(images, videos, audios)
        return self._get_mm_inputs(images, videos, audios, processor)


class Qwen2vlPlugin(BasePlugin):
    @override
    def _preprocess_image(self, image: "ImageObject", **kwargs) -> "ImageObject":
        image = super()._preprocess_image(image, **kwargs)
        if min(image.width, image.height) < 28:
            width, height = max(image.width, 28), max(image.height, 28)
            image = image.resize((width, height), resample=Image.Resampling.NEAREST)

        if image.width / image.height > 200:
            width, height = image.height * 180, image.height
            image = image.resize((width, height), resample=Image.Resampling.NEAREST)

        if image.height / image.width > 200:
            width, height = image.width, image.width * 180
            image = image.resize((width, height), resample=Image.Resampling.NEAREST)

        return image

    @override
    def _regularize_videos(self, videos: Sequence["VideoInput"], **kwargs) -> List[List["ImageObject"]]:
        results = []
        for video in videos:
            container = av.open(video, "r")
            video_stream = next(stream for stream in container.streams if stream.type == "video")
            sample_indices = self._get_video_sample_indices(video_stream, **kwargs)
            frames: List["ImageObject"] = []
            container.seek(0)
            for frame_idx, frame in enumerate(container.decode(video_stream)):
                if frame_idx in sample_indices:
                    frames.append(frame.to_image())

            if len(frames) % 2 != 0:  # qwen2-vl requires even number of frames
                frames.append(frames[-1])

            frames = self._regularize_images(frames, **kwargs)
            results.append(frames)

        return results
    
    @override
    def smart_resize(
        self, height: int, width: int, factor: int, min_pixels=256 * 28 * 28,max_pixels=1280 * 28 * 28,
    ):
        h_bar = round(height / factor) * factor
        w_bar = round(width / factor) * factor
        if h_bar * w_bar > max_pixels:
            beta = math.sqrt((height * width) / max_pixels)
            h_bar = math.floor(height / beta / factor) * factor
            w_bar = math.floor(width / beta / factor) * factor
        elif h_bar * w_bar < min_pixels:
            beta = math.sqrt(min_pixels / (height * width))
            h_bar = math.ceil(height * beta / factor) * factor
            w_bar = math.ceil(width * beta / factor) * factor
        return h_bar, w_bar

    @override
    def map_patch_numbers_batch(
        self,
        select_numbers_list,  # List of lists, each contains selected patch numbers for a sample
        h_bar_old_list,      # List of original heights after smart_resize
        w_bar_old_list,      # List of original widths after smart_resize
        h_bar_new_list,      # List of new heights after smart_resize
        w_bar_new_list,      # List of new widths after smart_resize
        old_merge=6,         # Original merge size (6 for 14x6 patches)
        new_merge=2,         # New merge size (2 for 14x2 patches)
        patch_size=14        # Patch size (default 14)
    ):
        """
        Maps selected patch numbers from the original patches (merge=6) to the new patches (merge=2) considering smart resizing.
        Processes each sample in the batch independently.
        """
        new_select_numbers = []
        
        for idx in range(len(select_numbers_list)):
            select_numbers = select_numbers_list[idx]
            h_bar_old = h_bar_old_list[idx]
            w_bar_old = w_bar_old_list[idx]
            h_bar_new = h_bar_new_list[idx]
            w_bar_new = w_bar_new_list[idx]
            
            # Original block parameters
            S_old = patch_size * old_merge  # 14*6=84
            merge_w_old = w_bar_old // S_old
            merge_h_old = h_bar_old // S_old
            
            # New block parameters
            S_new = patch_size * new_merge  # 14*2=28
            merge_w_new = w_bar_new // S_new
            merge_h_new = h_bar_new // S_new
            
            step_nums = []
            for nums in select_numbers:
                mapped_numbers = set()
                for num in nums:
                    # Calculate original block position
                    i = num // merge_w_old
                    j = num % merge_w_old
                    
                    # Original block coordinates (left-top to right-bottom)
                    x1_old = j * S_old
                    y1_old = i * S_old
                    x2_old = x1_old + S_old
                    y2_old = y1_old + S_old
                    
                    # Scale coordinates to new dimensions
                    x_scale = w_bar_new / w_bar_old
                    y_scale = h_bar_new / h_bar_old
                    
                    x1_new = x1_old * x_scale
                    y1_new = y1_old * y_scale
                    x2_new = x2_old * x_scale
                    y2_new = y2_old * y_scale
                    
                    # Determine new block indices
                    k_start = max(int(x1_new // S_new), 0)
                    k_end = min(int(math.floor((x2_new - 1e-9) / S_new)), merge_w_new - 1)
                    l_start = max(int(y1_new // S_new), 0)
                    l_end = min(int(math.floor((y2_new - 1e-9) / S_new)), merge_h_new - 1)
                    
                    # Collect all new blocks covered by the original block
                    for l in range(l_start, l_end + 1):
                        for k in range(k_start, k_end + 1):
                            new_num = l * merge_w_new + k
                            mapped_numbers.add(new_num)
                            
                step_nums.append(sorted(list(mapped_numbers)))
            new_select_numbers.append(step_nums)
        
        return new_select_numbers

    @override
    def process_messages(
        self,
        messages: Sequence[Dict[str, str]],
        images: Sequence["ImageInput"],
        videos: Sequence["VideoInput"],
        audios: Sequence["AudioInput"],
        processor: Optional["ProcessorMixin"],
    ) -> List[Dict[str, str]]:
        self._validate_input(images, videos, audios)
        image_processor: "BaseImageProcessor" = getattr(processor, "image_processor")
        merge_size: int = getattr(image_processor, "merge_size")
        merge_length = merge_size ** 2
        patch_size: int = getattr(image_processor, "patch_size")
        mm_inputs = self._get_mm_inputs(images, videos, audios, processor)
        image_grid_thw = mm_inputs.get("image_grid_thw", [])
        video_grid_thw = mm_inputs.get("video_grid_thw", [])
        interleave_token_num = 1

        num_image_tokens, num_video_tokens = 0, 0
        messages = deepcopy(messages)
        for message in messages:
            content = message["content"]
            while IMAGE_PLACEHOLDER in content:
                if num_image_tokens >= len(image_grid_thw):
                    raise ValueError(f"`len(images)` is less than the number of {IMAGE_PLACEHOLDER} tokens.")

                image_seqlen = image_grid_thw[num_image_tokens].prod() // merge_length if self.expand_mm_tokens else 1
                content = content.replace(
                    IMAGE_PLACEHOLDER, f"<|vision_start|>{self.image_token * image_seqlen}<|vision_end|>", 1
                )
                num_image_tokens += 1

            while VIDEO_PLACEHOLDER in content:
                if num_video_tokens >= len(video_grid_thw):
                    raise ValueError(f"`len(videos)` is less than the number of {VIDEO_PLACEHOLDER} tokens.")

                video_seqlen = video_grid_thw[num_video_tokens].prod() // merge_length if self.expand_mm_tokens else 1
                content = content.replace(
                    VIDEO_PLACEHOLDER, f"<|vision_start|>{self.video_token * video_seqlen}<|vision_end|>", 1
                )
                num_video_tokens += 1
                
            # interleave placeholder
            if INTERLEAVE_PLACEHOLDER in content:
                self.interleave_token = '<|quad_start|>'
                self.selected_token = '<|quad_end|>'
                
                if content.count(INTERLEAVE_PLACEHOLDER) % 2 != 0:
                    raise ValueError(f"The number of interleave placeholder must be an even number.")
                # import ipdb; ipdb.set_trace()
                content = content.split(INTERLEAVE_PLACEHOLDER)
                content_list = content[::2]
                interleave_list = content[1::2]
                
                # select_numbers
                select_numbers = []
                for interleave in interleave_list:
                    if interleave:
                        select_numbers.append([int(i.strip()) for i in interleave.split(",")])
                    else:
                        select_numbers.append([])
                select_numbers = [select_numbers] # for only one image
                
                orig_patch_size = 14
                orig_merge_size = 6
                factor = orig_patch_size * orig_merge_size
                resized_heights = []
                resized_widths = []
                new_image_heights = []
                new_image_widths = []
                for i, image in enumerate(images):
                    img = Image.open(image)
                    orig_width, orig_height = img.size
                    resized_height, resized_width = self.smart_resize(
                        orig_height,
                        orig_width,
                        factor,
                        min_pixels=256 * 28 * 28,
                        max_pixels=1280 * 28 * 28,
                    )
                    resized_heights.append(resized_height)
                    resized_widths.append(resized_width)
                    new_image_height = patch_size * int(image_grid_thw[i][1])
                    new_image_width = patch_size * int(image_grid_thw[i][2])
                    new_image_heights.append(new_image_height)
                    new_image_widths.append(new_image_width)
                
                # import ipdb; ipdb.set_trace()
                new_selected_numbers = self.map_patch_numbers_batch(
                    select_numbers,
                    resized_heights,
                    resized_widths,
                    new_image_heights,
                    new_image_widths,
                    orig_merge_size,
                    new_merge=merge_size,
                    patch_size=patch_size
                )
                
                interleaved_content = []
                for i, text in enumerate(content_list):
                    interleaved_content.append(text)
                    if i < len(interleave_list):
                        interleaved_content.append(self.interleave_token * interleave_token_num)
                        interleaved_content.append(f"<|vision_start|>{self.selected_token * len(new_selected_numbers[0][i])}<|vision_end|>")
                        # interleaved_content.append(self.selected_token * len(new_selected_numbers[0][i]))
                content = ''.join(interleaved_content)
                message["selected_numbers"] = new_selected_numbers

            message["content"] = content

        if len(images) != num_image_tokens:
            raise ValueError(f"The number of images does not match the number of {IMAGE_PLACEHOLDER} tokens.")

        if len(videos) != num_video_tokens:
            raise ValueError(f"The number of videos does not match the number of {VIDEO_PLACEHOLDER} tokens.")

        return messages
    
    @override
    def get_selected_mask(self, mm_inputs, selected_numbers, processor):
        image_processor: "BaseImageProcessor" = getattr(processor, "image_processor")
        merge_length: int = getattr(image_processor, "merge_size") ** 2
        
        mask_shape_0 = 0
        for batch_selected_numbers in selected_numbers:
            mask_shape_0 += len(batch_selected_numbers)
        selected_mask = torch.zeros(mask_shape_0, mm_inputs["pixel_values"].shape[0]//merge_length)
        
        row = 0
        column_base = 0
        for i, batch_selected_numbers in enumerate(selected_numbers):
            for j, step_selected_numbers in enumerate(batch_selected_numbers):
                for num in step_selected_numbers:
                    selected_mask[row, num+column_base] = 1
                row += 1
            column_base += mm_inputs["image_grid_thw"][i].prod().item()//merge_length
            
        return selected_mask

    @override
    def get_mm_inputs(
        self,
        images: Sequence["ImageInput"],
        videos: Sequence["VideoInput"],
        audios: Sequence["AudioInput"],
        imglens: Sequence[int],
        vidlens: Sequence[int],
        audlens: Sequence[int],
        batch_ids: Sequence[List[int]],
        batch_selected_numbers: Sequence[List[List[int]]],
        processor: Optional["ProcessorMixin"],
    ) -> Dict[str, Union[List[int], "torch.Tensor"]]:
        self._validate_input(images, videos, audios)
        mm_inputs = self._get_mm_inputs(images, videos, audios, processor)
        mm_inputs["selected_numbers"] = self.get_selected_mask(mm_inputs, batch_selected_numbers, processor)
        image_processor: "BaseImageProcessor" = getattr(processor, "image_processor")
        if "second_per_grid_ts" in getattr(image_processor, "model_input_names", []) and "video_grid_thw" in mm_inputs:
            video_fps = getattr(processor, "video_fps", 2.0)
            mm_inputs["second_per_grid_ts"] = [image_processor.temporal_patch_size / video_fps] * len(
                mm_inputs["video_grid_thw"]
            )

        return mm_inputs


class VideoLlavaPlugin(BasePlugin):
    @override
    def process_messages(
        self,
        messages: Sequence[Dict[str, str]],
        images: Sequence["ImageInput"],
        videos: Sequence["VideoInput"],
        audios: Sequence["AudioInput"],
        processor: Optional["ProcessorMixin"],
    ) -> List[Dict[str, str]]:
        self._validate_input(images, videos, audios)
        num_image_tokens, num_video_tokens = 0, 0
        messages = deepcopy(messages)
        mm_inputs = self._get_mm_inputs(images, videos, audios, processor)
        num_frames = 0
        has_images = "pixel_values_images" in mm_inputs
        has_videos = "pixel_values_videos" in mm_inputs
        if has_images or has_videos:
            if self.expand_mm_tokens:
                if has_images:
                    height, width = get_image_size(to_numpy_array(mm_inputs.get("pixel_values_images")[0]))
                    num_frames = 1

                if has_videos:
                    pixel_values_video = to_numpy_array(mm_inputs.get("pixel_values_videos")[0])
                    height, width = get_image_size(pixel_values_video[0])
                    num_frames = pixel_values_video.shape[0]  # frame dim is always after batch dim

                image_seqlen = (height // processor.patch_size) * (width // processor.patch_size) + 1
                video_seqlen = image_seqlen * num_frames
                if getattr(processor, "vision_feature_select_strategy", "default") == "default":
                    image_seqlen -= 1
            else:
                image_seqlen, video_seqlen = 1, 1

            for message in messages:
                content = message["content"]
                while IMAGE_PLACEHOLDER in content:
                    content = content.replace(IMAGE_PLACEHOLDER, "{{image}}" * image_seqlen, 1)
                    num_image_tokens += 1

                while VIDEO_PLACEHOLDER in content:
                    content = content.replace(VIDEO_PLACEHOLDER, "{{video}}" * video_seqlen, 1)
                    num_video_tokens += 1

                content = content.replace("{{image}}", self.image_token)
                message["content"] = content.replace("{{video}}", self.video_token)

        if len(images) != num_image_tokens:
            raise ValueError(f"The number of images does not match the number of {IMAGE_PLACEHOLDER} tokens.")

        if len(videos) != num_video_tokens:
            raise ValueError(f"The number of videos does not match the number of {VIDEO_PLACEHOLDER} tokens.")

        return messages

    @override
    def get_mm_inputs(
        self,
        images: Sequence["ImageInput"],
        videos: Sequence["VideoInput"],
        audios: Sequence["AudioInput"],
        imglens: Sequence[int],
        vidlens: Sequence[int],
        audlens: Sequence[int],
        batch_ids: Sequence[List[int]],
        processor: Optional["ProcessorMixin"],
    ) -> Dict[str, Union[List[int], "torch.Tensor"]]:
        self._validate_input(images, videos, audios)
        return self._get_mm_inputs(images, videos, audios, processor)


PLUGINS = {
    "base": BasePlugin,
    "llava": LlavaPlugin,
    "llava_next": LlavaNextPlugin,
    "llava_next_video": LlavaNextVideoPlugin,
    "minicpm_v": MiniCPMVPlugin,
    "mllama": MllamaPlugin,
    "paligemma": PaliGemmaPlugin,
    "pixtral": PixtralPlugin,
    "qwen2_audio": Qwen2AudioPlugin,
    "qwen2_vl": Qwen2vlPlugin,
    "video_llava": VideoLlavaPlugin,
}


def get_mm_plugin(
    name: str,
    image_token: Optional[str] = None,
    video_token: Optional[str] = None,
    audio_token: Optional[str] = None,
) -> "BasePlugin":
    plugin_class = PLUGINS.get(name, None)
    if plugin_class is None:
        raise ValueError(f"Multimodal plugin `{name}` not found.")

    return plugin_class(image_token, video_token, audio_token)
