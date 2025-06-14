export DEBUG_MODE="true" # Enable Debug if you want to see the rollout of model during RL
export LOG_PATH="./debug_log_2b.txt"

torchrun --nproc_per_node="8" \
    --nnodes="1" \
    --node_rank="0" \
    --master_addr="127.0.0.1" \
    --master_port="12345" \
    src/open_r1/grpo.py \
    --output_dir /opt/data/private/others/cxy/saves/grpo \
    --model_name_or_path /opt/data/private/others/cxy/models/Qwen2-VL-7B-Instruct \
    --dataset_name /opt/data/private/others/cxy/data/GEOQA_R1V_Train_8K \  #https://huggingface.co/datasets/leonardPKU/clevr_cogen_a_train
    --max_prompt_length 1024 \
    --per_device_train_batch_size 1 \
    --gradient_accumulation_steps 2 \
    --logging_steps 1 \
    --bf16 \
    --report_to wandb \
    --gradient_checkpointing false \
    --attn_implementation flash_attention_2 \
    --max_pixels 401408 \
    --num_train_epochs 2 \
    --run_name Qwen2-VL-2B-GRPO-GEOQA \
    --save_steps 100 \
    --save_only_model true