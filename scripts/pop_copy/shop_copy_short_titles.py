"""Connect the shared router to the installed internal material template."""
from pathlib import Path


def register(subs):
    titles = subs.add_parser('copy-short-titles', help='逐 SKU 比对并用信息分模块生成目标店官方导入表')
    for side in ('source', 'target'):
        titles.add_argument(f'--{side}-profile', required=True)
        titles.add_argument(f'--{side}-config', required=True, type=Path)
        titles.add_argument(f'--{side}-product-id', required=True, type=int)
    titles.add_argument('--history-dir', type=Path)
    titles.add_argument('--materials-root', type=Path)


def handle(args, adapter):
    from .entry import short_titles
    return short_titles(args, adapter)
