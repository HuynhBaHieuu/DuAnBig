#!/usr/bin/env python
import os
import sys


def main():
    # Console Windows mặc định cp1252 -> lỗi khi in tiếng Việt
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tracon.settings")
    from django.core.management import execute_from_command_line
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
