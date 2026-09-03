"""确认 SqliteSaver / AsyncPostgresSaver 精确调用方式。"""
import asyncio, inspect

def imp(path, attr):
    return getattr(__import__(path, fromlist=[attr]), attr)

print("=== SqliteSaver.from_conn_string return type ===")
from langgraph.checkpoint.sqlite import SqliteSaver
import tempfile, os
db = tempfile.NamedTemporaryFile(suffix=".db", delete=False); db.close()
val = SqliteSaver.from_conn_string(db.name)
print("type:", type(val).__name__)
print("is context manager? ", hasattr(val, "__enter__"))
# 用 with 用法测试
try:
    with SqliteSaver.from_conn_string(db.name) as saver:
        print("inside with -> type:", type(saver).__name__)
        print("has setup:", hasattr(saver, "setup"))
        if hasattr(saver, "setup"):
            try:
                r = saver.setup()
                print("setup() returned:", r)
                print("setup is coroutine?", inspect.iscoroutine(r) or inspect.iscoroutinefunction(getattr(saver, "setup")))
            except Exception as e:
                print("setup() err:", type(e).__name__, e)
except Exception as e:
    print("with err:", type(e).__name__, e)
os.unlink(db.name)

print("\n=== AsyncPostgresSaver setup signature ===")
AP = imp("langgraph.checkpoint.postgres.aio", "AsyncPostgresSaver")
print("has setup:", hasattr(AP, "setup"))
try:
    print("setup sig:", inspect.signature(AP.setup))
except Exception as e:
    print("setup sig err:", e)
print("setup is coroutinefunction?", inspect.iscoroutinefunction(getattr(AP, "setup", None)))
# __init__
try:
    print("__init__ sig:", inspect.signature(AP.__init__))
except Exception as e:
    print("__init__ sig err:", e)

print("\n=== InMemorySaver ===")
from langgraph.checkpoint.memory import InMemorySaver
ims = InMemorySaver()
print("InMemorySaver instance:", type(ims).__name__)
print("has setup:", hasattr(ims, "setup"))
print("has adelete_thread:", hasattr(ims, "adelete_thread"))
