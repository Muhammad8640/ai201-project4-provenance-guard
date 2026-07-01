@echo off
for /L %%i in (1,1,12) do (
  curl -s -o nul -w "%%i: %%{http_code}\n" -X POST http://127.0.0.1:5000/submit -H "Content-Type: application/json" -d "{\"text\": \"This is a test submission for rate limit testing purposes only.\", \"creator_id\": \"ratelimit-test\"}"
)