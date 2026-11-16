# Day 7 — Agentic RAG: hướng dẫn chạy

Agent tìm bằng chứng trong kho tài liệu, trả lời có nguồn và từ chối khi thiếu bằng
chứng. Bốn phương pháp retrieval, agent dùng model thật, benchmark dev/test và các
thử nghiệm cải thiện được đo trước–sau.

> Tài liệu đề bài: [README](README.md) · [Hướng dẫn](GUIDE.md) · [Benchmark](BENCHMARK.md) ·
> [Rubric](RUBRIC.md) · [Báo cáo](report/individual/MSSV_HoTen.md)

---

## 1. Thiết lập (chạy 1 lần)

### 1.1 Python 3.11+

```bash
winget install Python.Python.3.11
py -3.11 -m venv .venv
.venv/Scripts/activate
python --version        # phải là 3.11 trở lên
```

### 1.2 Cài thư viện

Cài **torch CPU trước** để tránh tải bản CUDA ~2.5GB:

```bash
pip install torch --index-url https://download.pytorch.org/whl/cpu
python -c "import torch; print(torch.__version__)"
```

Sau đó:

```bash
pip install -r requirements.txt
```

> Nếu `import torch` báo "Application Control policy has blocked this file" →
> máy đang chặn DLL của torch. Khi đó dùng `EMBED_BACKEND=api` (xem 1.3).

### 1.3 Cấu hình `.env`

```bash
copy .env.example .env
```

| Biến | Ý nghĩa |
|---|---|
| `LLM_BASE_URL` | Endpoint OpenAI-compatible. Có thể là endpoint riêng của bạn. |
| `LLM_API_KEY` | API key (nếu endpoint không cần thì để trống cũng được). |
| `LLM_MODEL` | Tên model, ví dụ `gpt-4o-mini`. |
| `EMBED_BACKEND` | `local` (mặc định) hoặc `api`. |
| `EMBED_MODEL` | `BAAI/bge-m3` (mặc định). |

Embedding chạy **local** thì lần đầu tải `BAAI/bge-m3` (~2.2GB) về
`~/.cache/huggingface` và mất vài phút. Nếu endpoint của bạn đã phục vụ bge-m3,
đặt `EMBED_BACKEND=api` và khai báo `EMBED_BASE_URL` — không cần torch.

---

## 2. Chạy pipeline

Mỗi bước là một lệnh riêng, chạy theo thứ tự. Bước 1–4 không tốn tiền; bước 5
gọi model và là bước duy nhất có chi phí.

```bash
# 0. Ghép bộ benchmark (cần file questions.json của Day 6)
python scripts/00_build_questions.py --day6 "<đường dẫn>/K5-L3B-DAY06-.../benchmark/questions.json"

# 1. Đọc PDF -> pages + manifest
python scripts/01_ingest.py

# 2. Pages -> chunks
python scripts/02_chunk.py --strategy structure

# 3. Embed bằng bge-m3 + lưu vào Chroma
python scripts/03_embed_store.py

# 4. Benchmark 4 phương pháp retrieval trên DEV
python scripts/04_benchmark.py --method all --split dev
```

Kiểm tra `data/manifest.json`: phải có **36 physical pages / 30 content pages**
và 6 trang bị loại kèm lý do.

---

## 3. Chọn cấu hình trên dev, rồi mới chạy test

`BENCHMARK.md` yêu cầu chốt cấu hình trên dev **trước** khi chạy test.

```bash
python scripts/04_benchmark.py --method all --split dev
```

Chọn theo tiêu chí công bố trước (ghi vào báo cáo), rồi mới chạy test:

```bash
python scripts/04_benchmark.py --method dense --split test     # baseline
python scripts/04_benchmark.py --method hybrid --split test    # cấu hình đã chọn
```

---

## 4. Chạy agent

```bash
python scripts/05_run_agent.py --split dev --config dense
python scripts/05_run_agent.py --split dev --config hybrid

# Sau khi chốt cấu hình: chạy trên test
python scripts/05_run_agent.py --split test --config hybrid
```

Mỗi lần chạy ghi ra:

```
results/agent-<config>-<split>/
  traces.json             trace đầy đủ: tool call, context, câu trả lời, citation, token, lỗi
  cases.json              danh sách case cho script tổng hợp
  reviews.template.json   KHUNG nhãn — điền bằng tay
```

### 4.1 Chấm tay (bắt buộc)

`reviews.template.json` **phải được điền bằng tay**: mở `traces.json`, đọc
evidence trong `context`, rồi điền:

- Câu **có đáp án**: `answer_grade` 0/1/2 (0 = sai hoặc thiếu evidence;
  1 = đúng một phần; 2 = đúng và đủ) và `citation_support` — một boolean cho
  **mỗi** citation, đánh dấu citation đó có thực sự hỗ trợ câu trả lời không.
- Câu **thiếu evidence**: `abstention_correct` (true/false).

`citation_support` phải có đúng số phần tử bằng số citation của câu đó.
Lưu thành `reviews.json` rồi chạy stage 6:

```bash
python scripts/06_metrics.py --config hybrid --split dev
```

---

## 5. Cấu trúc mã nguồn

| File | Vai trò |
|---|---|
| `src/textutil.py` | Chuẩn hóa Unicode, so khớp evidence, tokenize cho BM25 |
| `src/models.py` | `DocumentPage`, `Chunk`, danh sách trường filter được |
| `src/ingestion.py` | PDF → pages; ngưỡng font **tự suy ra từ chính tài liệu** |
| `src/chunking.py` | `structure` (theo heading) và `fixed` (cửa sổ ký tự) |
| `src/embeddings.py` | bge-m3, backend `local` hoặc `api` |
| `src/store.py` | ChromaDB persistent, cosine, filter theo metadata |
| `src/retrievers.py` | `dense`, `bm25`, `hybrid` (RRF), `rerank` |
| `src/benchmark.py` | Schema câu hỏi, gold matching, Hit@1 / Hit@3 / Context Rate |
| `src/llm.py` | Client OpenAI-compatible, parse JSON có fallback |
| `src/agent.py` | Controller + retriever adapter trên `starter/agentic.py` |

Vòng lặp agent dùng nguyên vẹn `starter/agentic.py` — không sửa file starter.

---

## 6. Bốn phương pháp retrieval

| Tên | Cách tìm | Vì sao khác nhau |
|---|---|---|
| `dense` | Embed query bằng bge-m3, tìm vector gần nhất | Khớp ngữ nghĩa |
| `bm25` | Okapi BM25 (tự viết, k1=1.5 b=0.75) | Khớp từ khóa chính xác ("811 million") |
| `hybrid` | RRF của dense + bm25, `score = Σ 1/(60 + rank)` | Cả hai, không cần chuẩn hóa score |
| `rerank` | Cross-encoder `ms-marcho-MiniLM-L-6-v2` đọc query+chunk cùng lúc | Xếp lại bằng quan hệ query–chunk |

---

## 7. Về benchmark: dev và test

Bộ 50 câu của Day 6 đã từng chạy qua retrieval và dùng để chọn chiến lược
chunking, nên **không câu nào của Day 6 còn sạch** để làm test của Day 7.

- **dev** (15 có đáp án + 2 thiếu evidence): lấy từ Day 6 (gold quote đã kiểm
  chứng tay) + 2 câu thiếu evidence viết mới.
- **test** (5 có đáp án + 2 thiếu evidence): **viết mới hoàn toàn** từ PDF, và
  không trùng với bất kỳ câu nào của Day 6.

`data/questions.json` ghi `provenance` cho từng câu để báo cáo nói rõ câu nào
đã bị "contaminate", câu nào thực sự held out.

---

## 8. Lỗi thường gặp

| Lỗi | Cách xử lý |
|---|---|
| `ModuleNotFoundError: fitz` | `pip install pymupdf` |
| `import torch` bị chặn bởi policy | Đặt `EMBED_BACKEND=api` |
| Mô hình bge-m3 tải lỗi | Kiểm tra mạng; đổi sang `EMBED_BACKEND=api` |
| Context luôn rỗng, agent toàn abstain | Chunk 700 ký tự + `--budget 2800`; xem `context_chars` trong trace |
| `LLM_MODEL chưa được đặt` | Điền `.env` |
| Model trả JSON trong code fence | Đã xử lý trong `src/llm.py` (`parse_json_object`) |

---

## 9. Chạy test

```bash
python -m unittest discover -s tests -v
```

Test offline, không cần model, không cần vector store, không cần mạng.
