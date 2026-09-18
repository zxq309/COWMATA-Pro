"""Shared cancellation and archive path containment checks."""
from pathlib import Path


def check(cancelled):
    if cancelled():
        raise InterruptedError('已取消；未完成的包不会标记为已派发')


def safe_path(root, relative):
    if not isinstance(relative, str) or not relative or '\\' in relative:
        raise ValueError('压缩包路径格式无效')
    parts = relative.split('/')
    reserved = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}
    if any(p in {'', '.', '..'} or p.endswith(('.', ' ')) or any(c in p for c in ':<>|?*')
           or any(ord(c) < 32 for c in p) or p.split('.')[0].upper() in reserved for p in parts):
        raise ValueError('压缩包含越界或不安全路径：' + relative)
    root = Path(root).resolve()
    target = root.joinpath(*parts)
    for part in (target, *target.parents):
        if part == root:
            break
        if part.is_symlink() or getattr(part, 'is_junction', lambda: False)():
            raise ValueError('路径不能穿过符号链接或目录联接')
    if not target.resolve().is_relative_to(root):
        raise ValueError('路径超出牧场目录')
    return target
