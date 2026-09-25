"""药店健康陪伴边界的本地 JSON 命令入口。"""
import json
import sys
from pathlib import Path

from .service import Service


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "validate":
        payload = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
        print(json.dumps(Service().register(payload), ensure_ascii=False, sort_keys=True))
        return 0
    print(json.dumps(Service().health(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
