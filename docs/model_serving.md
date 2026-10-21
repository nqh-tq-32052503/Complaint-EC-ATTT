# Hướng Dẫn Serving Mô Hình AI (Model Serving)

Tài liệu hướng dẫn triển khai và phục vụ (serving) 2 mô hình AI cốt lõi sử dụng vLLM trên hạ tầng máy chủ GPU:
1. **Surya OCR & Layout Detection** (`datalab-to/surya-ocr-2`)
2. **Gemma 4 Language Model** (`google/gemma-4-26B-A4B-it`)

---

## 1. Môi Trường & Chuẩn Bị

Kích hoạt môi trường Conda hỗ trợ vLLM & CUDA:

```bash
conda activate env
```

---

## 2. Phục Vụ Mô Hình Surya OCR (Port 8043)

### 2.1. Cấu hình biến môi trường Surya

```bash
export SURYA_INFERENCE_BACKEND=vllm
export SURYA_INFERENCE_URL=http://127.0.0.1:8043/v1
export SURYA_INFERENCE_AUTOSTART=false
export SURYA_INFERENCE_PARALLEL=64
export TORCH_DEVICE=cuda:0
```

### 2.2. Khởi chạy vLLM cho Surya OCR

Khởi chạy tiến trình nền trên GPU 0:

```bash
nohup env CUDA_VISIBLE_DEVICES=0 HF_TOKEN=your_hf_token \
vllm serve datalab-to/surya-ocr-2 \
    --port 8043 \
    --host 127.0.0.1 \
    --dtype bfloat16 \
    --max-model-len 18000 \
    --gpu-memory-utilization 0.3 \
    --max-num-batched-tokens 8192 \
    --api-key EMPTY \
    --trust-remote-code \
    --max-num-seqs 256 \
    > surya_ocr.log 2>&1 &
```

---

## 3. Phục Vụ Mô Hình LLM Gemma-4-26B-A4B (Port 8076)

### Cách 1: Khởi chạy bằng Docker Container (Khuyên dùng)

```bash
docker run -d --name vllm-gemma4-26b-a4b --gpus all \
  -e CUDA_VISIBLE_DEVICES=1 \
  -p 8076:8076 \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  --shm-size 16g \
  -e HUGGING_FACE_HUB_TOKEN=your_hf_token \
  vllm/vllm-openai:v0.11.1 \
  --model google/gemma-4-26B-A4B-it \
  --host 127.0.0.1 \
  --tensor-parallel-size 1 \
  --kv-cache-dtype auto \
  --enable-prefix-caching \
  --enable-chunked-prefill \
  --reasoning-parser gemma4 \
  --max-num-seqs 256 \
  --max-num-queued-reqs 512 \
  --max-num-batched-tokens 8192 \
  --max-model-len 64000 \
  --gpu-memory-utilization 0.6 \
  --trust-remote-code
```

### Cách 2: Khởi chạy trực tiếp bằng lệnh `nohup` trên Host

```bash
nohup env HF_TOKEN=your_hf_token CUDA_VISIBLE_DEVICES=1 HF_HOME=/home/jovyan/scratch/datmq3/huggingface \
vllm serve google/gemma-4-26B-A4B-it \
  --host 127.0.0.1 \
  --tensor-parallel-size 1 \
  --kv-cache-dtype auto \
  --enable-prefix-caching \
  --enable-chunked-prefill \
  --reasoning-parser gemma4 \
  --max-num-seqs 256 \
  --max-num-queued-reqs 512 \
  --max-num-batched-tokens 8192 \
  --max-model-len 64000 \
  --gpu-memory-utilization 0.6 \
  --port 8076 \
  --trust-remote-code \
  > gemma_a4b.log 2>&1 &
```

---

## 4. Tích Hợp Vào File `.env` của Complaint-EC

Sau khi khởi chạy thành công 2 model, cấu hình URL tương ứng trong `.env`:

```dotenv
LLM_GATEWAY_URL=http://host.docker.internal:8076/v1
LLM_GATEWAY_API_KEY=empty
LLM_MODEL_NAME=google/gemma-4-26B-A4B-it
```
