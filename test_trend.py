#!/usr/bin/env python3
import sys; sys.path.insert(0, ".")
from dotenv import load_dotenv; load_dotenv()
from src.trend.multi_tf import build_trend_context

for t in ["GLD", "TLT", "BDRY"]:
    print(f"\n=== {t} ===")
    print(build_trend_context(t).to_prompt_block())
