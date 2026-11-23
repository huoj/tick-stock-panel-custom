"""测试套件的全局兜底隔离。

**为什么要有这个文件**：`preferences.json` 是真实用户配置，而很多测试在
import `app.main` / 调用设置 API / 走 `boot_check` 时会**真的去读写它**。
后果不是"测试脏一点"，而是把生产状态改坏：

- 2026-11-09、11-12 下午、11-12 深夜、11-13 上午，**四次**把真实的
  `realtime_quotes_enabled` 写成 `false`，每次都让实时行情静默停摆，
  而且没有任何报错。
- 更严重的一次：测试里的 mock 占住了 polars collect 闸，watchdog 判定
  `backend wedged`，把**真实服务进程**退掉（2026-11-13 00:02）。

设计原则（第一版踩过的坑）：
**只兜底"没人管"的路径，绝不覆盖测试显式设置的目录**。
`test_data_dir_upgrade` / `test_config_backup` / `test_atomic_parquet_writes`
这类用例本身就在验证"自定义数据目录"的语义，一旦被 autouse 抢走
`settings.data_dir`，它们会集体失真（实测 10 条假失败）。

因此这里不 monkeypatch `settings.data_dir`，只在**没有可用数据目录**
（即 `settings.data_dir` 指向仓库真实 `data/`）时把 preferences 落盘
重定向到 tmp —— 这正是"污染真实用户配置"的唯一入口。
"""
from __future__ import annotations

from pathlib import Path

import pytest


def _points_at_real_data_dir(data_dir: Path) -> bool:
    """`settings.data_dir` 是否仍指向仓库真实 `data/`（而非某个 tmp）。"""
    try:
        return data_dir.resolve() == (Path(__file__).resolve().parents[1] / "data").resolve()
    except OSError:
        return False


@pytest.fixture(autouse=True)
def _isolate_user_state(tmp_path, monkeypatch):
    """只在测试没接管数据目录时，把 preferences 落盘重定向到 tmp。

    覆盖范围刻意"够用即止"：只隔离真正会破坏生产状态的那条路径
    （`preferences._path()` → `<data_dir>/user_data/preferences.json`）。
    测试自己 monkeypatch 了 `_path` 或 `settings.data_dir` 时，
    本 fixture 直接让路，避免与用例语义打架。
    """
    from app.config import settings
    from app.services import preferences

    # 测试已自行接管数据目录 → 尊重它，不插手
    # (注意: 这里也必须 yield —— 生成器 fixture 一次都不 yield 会让 pytest 报
    #  "did not yield a value", 73 条 ERROR 就是这么来的)
    if not _points_at_real_data_dir(Path(settings.data_dir)):
        yield
        return

    prefs_path = tmp_path / "isolated_user_data" / "preferences.json"
    prefs_path.parent.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(preferences, "_path", lambda: prefs_path)
    # preferences 有进程内 (mtime,size) 缓存 —— 换路径必须一并清，
    # 否则会命中旧缓存把真实值读出来、甚至写回去。
    monkeypatch.setattr(preferences, "_cache", None, raising=False)
    monkeypatch.setattr(preferences, "_cache_sig", None, raising=False)

    yield

    preferences._invalidate_cache()


@pytest.fixture(autouse=True)
def _no_real_watchdog(monkeypatch):
    """测试进程不启动健康 watchdog。

    `import app.main` 会连带启动 watchdog 线程，而测试里的 mock 很容易占住
    polars collect 闸；probe 连续超时两次就会调 `exit_cb` → `os._exit`，
    把跑测试的进程一起带走（2026-11-13 00:02 实测发生过）。

    关它要关在**开关**上，而不是 patch `watchdog.start_watchdog`：
    `main.py` 里是 `from app.watchdog import start_watchdog` 的**局部导入**，
    patch 模块属性对它无效（第一版这么写，实测 36 条 watchdog 报错依旧）。
    `start_watchdog` 第一行就 `if not settings.watchdog_enabled: return None`，
    关掉这个 settings 字段才真正拦住。
    """
    from app.config import settings

    monkeypatch.setattr(settings, "watchdog_enabled", False, raising=False)
