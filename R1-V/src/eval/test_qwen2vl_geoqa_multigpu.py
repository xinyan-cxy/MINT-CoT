from transformers import Qwen2VLForConditionalGeneration, AutoTokenizer, AutoProcessor, Qwen2_5_VLForConditionalGeneration
from qwen_vl_utils import process_vision_info
import torch
import json
import tqdm
from math_verify import parse, verify
import argparse
import pandas as pd
from torch.multiprocessing import Process, set_start_method, Manager
from transformers.utils.logging import disable_progress_bar
disable_progress_bar()
import re

def extract_answer(content):
    # 统一匹配三种前缀，查找后面第一个大写字母
    pattern = re.compile(r'(?:Answer:|The answer is|the answer is|### The final answer is:)\s*[^A-D]*([A-D])', re.IGNORECASE)
    match = pattern.search(content)
    if match:
        return match.group(1)
    return content

# >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
# >>>>> 1. get evaluation configuration <<<<<
# >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
def get_eval_config():
    parser = argparse.ArgumentParser(description="Inference script for GeoQA evaluation.")
    parser.add_argument("--model_path", default="/opt/data/private/others/cxy/saves/grpo/53k_resacle50/checkpoint-700", type=str, help="Path to the model checkpoint (e.g., qwen2vl model or a fine-tuned model).")
    parser.add_argument("--batch_size", default=1, type=int, help="Batch size for inference. Reduce if GPU OOM (default: 50).")
    parser.add_argument("--output_path", default="/opt/data/private/others/cxy/eval_outputs/outputs_debug/geoqa/grpo_53k_rescale50_step700_log.json", type=str, help="Path to save inference result (e.g., JSON file).")
    parser.add_argument("--prompt_path", default="./prompts/geoqa_test_prompts_new.jsonl", type=str, help="Path to the prompts JSONL file for GeoQA evaluation.")
    all_gpu = ",".join(map(str, range(torch.cuda.device_count())))
    parser.add_argument("--gpu_ids", default=all_gpu, help="comma-separated list of GPU IDs to use")
    args = parser.parse_args()
    return args

# >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
# >>>>>>>>>> 2. load testset <<<<<<<<<<<<<
# >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
def prepare_test_messages(testset_path):
    testset_data = pd.read_json(testset_path, lines=True).to_dict(orient="records")
    QUESTION_TEMPLATE = "{Question} Output the thinking process in <think> </think> and final answer (number) in <answer> </answer> tags."
    REASONING_PROMPT_RATIONALE = """Provide a rationale to analyze the question.
    Next, generate a step-by-step reasoning process to solve the problem. Ensure the steps are logical and concise.
    Finally, provide a concise summary of the final answer in the following format: 'The final answer is: xxx.

    Format your response with the following sections, separated by ###:
    ### Rationales:
    ### Let's think step by step.
    ### Step 1:
    ### Step 2:
    ...
    ### The final answer is: 

    {Question}"""

    REASONING_PROMPT_COT = """You are given a math problem image, please solve the problem step by step.
    {Question}"""

    tested_messages = []
    for i in testset_data:
        message = [{
            "role": "user",
            "content": [
                {
                    "type": "image", 
                    "image": f"file://{i['image_path']}"
                },
                {
                    "type": "text",
                    "text": REASONING_PROMPT_RATIONALE.format(Question=i['question'])
                }
            ]
        }]
        tested_messages.append(message)
    return testset_data, tested_messages




# >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
# >>>>> 3. use several GPUs to accelerate inference at testset <<<<<
# >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>

def init_model(model_path, gpu_id):
    """init a model(args.model_path) on a specific gpu"""
    # We recommend enabling flash_attention_2 for better acceleration and memory saving, especially in multi-image and video scenarios.
    model = Qwen2VLForConditionalGeneration.from_pretrained(
    # model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
        model_path,
        torch_dtype=torch.bfloat16,
        attn_implementation="flash_attention_2",
        device_map=f"cuda:{gpu_id}",
    )

    # default processer
    processor = AutoProcessor.from_pretrained(model_path, use_fast=True)
    return model, processor

def answer_a_batch_question_qwen(batch_messages, model, processor):
    """ let qwen answer a batch of questions """
    text = [processor.apply_chat_template(msg, tokenize=False, add_generation_prompt=True) for msg in batch_messages]        
    image_inputs, video_inputs = process_vision_info(batch_messages)
    inputs = processor(
        text=text,
        images=image_inputs,
        videos=video_inputs,
        padding=True,
        return_tensors="pt",
    )
    inputs = inputs.to(model.device)
    inputs.update({
            "interleave_inf": True,
            "selected_numbers": None,
            "num_added_tokens": None,
            "predicted_labels": None,
            "new_input_ids": None,
            "predicted_labels_output": None,
            "predict_threshold": 0.7,
            "interleave_sim": None,
            "interleave_cache": {},
            "return_dict_in_generate": True,
        })
    
    # generated_ids = model.generate(**inputs, max_new_tokens=1024, do_sample=False, use_cache=True)
    generated = model.generate(**inputs, max_new_tokens=1024, do_sample=False, use_cache=True)
    generated_ids = generated.sequences
    predicted_labels = generated.predicted_labels
    
    # generated_ids = model.generate(**inputs, use_cache=True, max_new_tokens=1024) # do_sample=False
    generated_ids_trimmed = [
        out_ids[len(in_ids):] for in_ids, out_ids in zip(inputs.input_ids, generated_ids)
    ]
    batch_output_text = processor.batch_decode(
        generated_ids_trimmed, skip_special_tokens=True, clean_up_tokenization_spaces=False
    )
    return batch_output_text

def infer_on_single_gpu(model_path, device_id, chunk_of_tested_messages, batch_size, results=None):
    """init model on this single gpu and let it answer asign chunk of questions"""
    model, processor = init_model(model_path, device_id)
    
    ### split batch
    responses = []
    batch_messages_list = [chunk_of_tested_messages[start: start + batch_size] 
               for start in range(0, len(chunk_of_tested_messages), batch_size)]

    for batch_messages in tqdm.auto.tqdm(batch_messages_list, desc=f"GPU {device_id} progress", position=device_id, leave=False):
        batch_output_text = answer_a_batch_question_qwen(batch_messages, model, processor)
        
        responses.extend(batch_output_text)
    
    results[device_id] = responses
    return
        
        
def multi_gpu_inference(prompts, gpu_ids, model_path, batch_size):
    """ let each gpu (along with a model) answer a chunk of questions """
    set_start_method("spawn", force=True)
    manager = Manager()
    gpu_id2result = manager.dict()

    gpu_ids = [int(gpu_id.strip()) for gpu_id in gpu_ids.split(',')]
    num_gpus = len(gpu_ids)

    chunk_size = len(prompts) // num_gpus
    processes = []
    for i, gpu_id in enumerate(gpu_ids):
        start_idx = i * chunk_size
        end_idx = (i + 1) * chunk_size if i != num_gpus - 1 else len(prompts)
        chunk = prompts[start_idx: end_idx]
        process = Process(target=infer_on_single_gpu, args=(model_path, gpu_id, chunk, batch_size, gpu_id2result))
        process.start()
        processes.append(process)

    # for process in tqdm.auto.tqdm(processes, desc="Inference progress", position=num_gpus, leave=True):
    for process in processes:
        process.join()

    all_predicts = []
    for gpu_id in gpu_ids:
        all_predicts.extend(gpu_id2result[gpu_id])

    return all_predicts

# >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
# >>>>>>>>>> 4. compute metrics <<<<<<<<<<<
# >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>

def compute_metrics(testset_data, all_predicts):
    final_output = []
    correct_number = 0

    for input_example, model_output in zip(testset_data, all_predicts):
        model_answer = extract_answer(model_output)
        ground_truth = input_example['ground_truth'] 

        # Count correct answers
        if model_answer is not None and model_answer == ground_truth:
            correct_number += 1
            is_correct = True
        else:
            is_correct = False
        
        try:
            result = {
                'question': input_example,
                'ground_truth': ground_truth,
                'model_output': model_output,
                'extracted_answer':str(model_answer[0]) if model_answer is not None else None,
                'is_correct':is_correct
            }

        except Exception as e:
            print("no answer parsed",e,model_answer)
            result = {
                'question': input_example,
                'ground_truth': ground_truth,
                'model_output': model_output,
                'extracted_answer':None,
                'is_correct':is_correct
            }



        final_output.append(result)


    # Calculate and print accuracy
    accuracy = correct_number / len(tested_messages) * 100
    print(f"\nAccuracy: {accuracy:.2f}%")

    # Save results to a JSON file
    with open(args.output_path, "w") as f:
        json.dump({
            'accuracy': accuracy,
            'results': final_output
        }, f, indent=2, ensure_ascii=False)

    print(f"Results saved to {args.output_path}")



if __name__ == "__main__":
    args = get_eval_config()
    testset_data, tested_messages = prepare_test_messages(testset_path=args.prompt_path)
    all_predicts = multi_gpu_inference(tested_messages, args.gpu_ids, args.model_path, args.batch_size)
    compute_metrics(testset_data, all_predicts)

