# Verification contract

Chạy từ thư mục gốc của repository, theo đúng thứ tự dưới đây.

- Build: `python3 -m compileall -q hooks tests`
- Typecheck: `N/A: Python stdlib project chưa dùng static type checker`
- Lint: `python3 tests/static_check.py`
- Test: `python3 -m unittest discover -s tests -p 'test_*.py' -v`

## Project-specific rules

- Plugin root, manifest `.claude-plugin/plugin.json`, thư mục `agents` và thư mục
  `skills` phải pass `claude plugin validate ... --strict`.
- Version trong `.claude-plugin/plugin.json` phải đồng bộ với version của plugin
  tương ứng trong `.claude-plugin/marketplace.json` khi marketplace còn khai báo
  trường `version`.
- Không được track file `*.pyc` trong Git.
- `ATTEMPT CAP = 3` cho từng bước build, lint và test. Chạm trần vẫn fail thì dừng
  và báo output thật; không được báo pass.
