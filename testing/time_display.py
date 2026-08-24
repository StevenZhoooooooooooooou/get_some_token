#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
简单的时间显示工具
显示当前日期和时间的多种格式
"""

import datetime


def display_current_time():
    """显示当前时间的多种格式"""
    now = datetime.datetime.now()
    
    print("=" * 50)
    print("当前时间显示工具")
    print("=" * 50)
    print(f"标准格式: {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"日期: {now.strftime('%Y年%m月%d日')}")
    print(f"时间: {now.strftime('%H时%M分%S秒')}")
    print(f"星期: {now.strftime('%A')}")
    print(f"ISO格式: {now.isoformat()}")
    print(f"时间戳: {now.timestamp()}")
    print("=" * 50)


def main():
    """主函数"""
    display_current_time()


if __name__ == "__main__":
    main()
