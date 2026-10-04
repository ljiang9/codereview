"""`python -m codereview` 入口。"""
import sys

try:
    from .codereview import main
except ImportError:  # 直接 python __main__.py 运行时的 fallback
    from codereview import main

if __name__ == "__main__":
    sys.exit(main())
