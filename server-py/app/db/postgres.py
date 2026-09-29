"""PostgreSQL 连接配置 + 异步连接池

两套用法，互不干扰：

1. `PG_CONNECTION_STRING`（psycopg 方言）—— 给 langchain_postgres 的 PGVector 用，
   知识库向量检索走它；
2. 异步连接池（init_pool / get_pool）—— 给全链路追踪落库用（app/observability/trace.py）。
   追踪是"记录"，绝不能因为写库把用户请求拖住、更不能把业务带崩，所以单独一个池、
   连不上就静默降级（追踪失效，业务照常）。

池必须在 FastAPI startup 里创建：asyncpg 的连接绑定正在运行的事件循环。
"""
import logging
import os
from typing import Optional
from urllib.parse import quote_plus

PG_HOST = os.getenv("PG_HOST", "localhost")
PG_PORT = os.getenv("PG_PORT", "5432")
PG_USER = os.getenv("PG_USER", "postgres")
PG_PASSWORD = os.getenv("PG_PASSWORD", "postgres")
PG_DATABASE = os.getenv("PG_DATABASE", "jisu_ai")

# psycopg3 连接串，供 langchain_postgres PGVector 使用
PG_CONNECTION_STRING = (
    f"postgresql+psycopg://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{PG_DATABASE}"
)

# asyncpg 连接串（标准 libpq DSN），密码里有特殊字符也能对上
PG_DSN = (
    f"postgresql://{quote_plus(PG_USER)}:{quote_plus(PG_PASSWORD)}"
    f"@{PG_HOST}:{PG_PORT}/{PG_DATABASE}"
)

POOL_MIN_SIZE = int(os.getenv("PG_POOL_MIN_SIZE", "1"))
POOL_MAX_SIZE = int(os.getenv("PG_POOL_MAX_SIZE", "4"))

logger = logging.getLogger("jisu.postgres")

_pool: Optional[object] = None


def get_pool():
    """取连接池；没建起来（没配 PG / PG 挂了）时返回 None，调用方据此跳过落库"""
    return _pool


def pool_ready() -> bool:
    return _pool is not None


async def init_pool() -> Optional[object]:
    """创建连接池。失败**不抛异常**：数据库不可用时服务照常启动，只是追踪不落库。"""
    global _pool
    if _pool is not None:
        return _pool
    try:
        import asyncpg

        _pool = await asyncpg.create_pool(
            dsn=PG_DSN,
            min_size=POOL_MIN_SIZE,
            max_size=POOL_MAX_SIZE,
            timeout=float(os.getenv("PG_CONNECT_TIMEOUT", "5")),
            command_timeout=float(os.getenv("PG_COMMAND_TIMEOUT", "10")),
        )
        logger.info("PostgreSQL 连接池已就绪（%s:%s/%s）", PG_HOST, PG_PORT, PG_DATABASE)
    except Exception as err:  # noqa: BLE001 - 连不上就是"追踪不可用"，不能拦住启动
        _pool = None
        logger.warning("PostgreSQL 连接池创建失败，全链路追踪将不落库（业务不受影响）：%s", err)
    return _pool


async def close_pool() -> None:
    """关闭连接池（服务退出时调用，别让连接悬着）"""
    global _pool
    if _pool is None:
        return
    try:
        await _pool.close()
    except Exception as err:  # noqa: BLE001
        logger.warning("关闭 PostgreSQL 连接池出错：%s", err)
    finally:
        _pool = None
