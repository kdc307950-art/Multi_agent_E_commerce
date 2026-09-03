"""LangGraph 1.2.11 第二轮验收：checkpointer + interrupt + AsyncPostgresSaver 接口。"""
import sys, asyncio, inspect
print("python:", sys.version)
results = []

def check(name, fn):
    try:
        results.append((name, "OK", fn()))
    except Exception as e:
        results.append((name, "FAIL", f"{type(e).__name__}: {e}"))

def imp(path, attr):
    return getattr(__import__(path, fromlist=[attr]), attr)

# SqliteSaver
check("SqliteSaver import", lambda: imp("langgraph.checkpoint.sqlite", "SqliteSaver"))
check("SqliteSaver.from_conn_string", lambda: hasattr(imp("langgraph.checkpoint.sqlite", "SqliteSaver"), "from_conn_string"))
check("SqliteSaver.setup", lambda: hasattr(imp("langgraph.checkpoint.sqlite", "SqliteSaver"), "setup"))

# AsyncPostgresSaver 接口
def pg_iface():
    AP = imp("langgraph.checkpoint.postgres.aio", "AsyncPostgresSaver")
    methods = ["from_conn_string", "setup", "asetup", "aput", "aput_writes",
               "aget_tuple", "alist", "adelete_thread"]
    missing = [m for m in methods if not hasattr(AP, m)]
    # 记录关键签名
    sigs = {}
    for m in ["from_conn_string", "aput", "aput_writes", "aget_tuple", "alist", "adelete_thread"]:
        try:
            sigs[m] = str(inspect.signature(getattr(AP, m)))
        except Exception as e:
            sigs[m] = f"<no-sig: {e}>"
    return {"missing": missing, "sigs": sigs}
check("AsyncPostgresSaver interface", pg_iface)

# interrupt + Command resume with checkpointer
def hitl_test():
    from langgraph.graph import StateGraph, START, END
    from langgraph.types import interrupt, Command
    from langgraph.checkpoint.sqlite import SqliteSaver
    from typing import TypedDict, Annotated
    from langgraph.graph.message import add_messages

    class S(TypedDict, total=False):
        messages: Annotated[list, add_messages]
        out: str

    def ask(state):
        resp = interrupt({"question": "approve?", "amount": 10})
        return {"out": "approved" if resp.get("approved") else "rejected"}

    def fin(state):
        return {"out": state.get("out", "done")}

    b = StateGraph(S)
    b.add_node("ask", ask)
    b.add_node("fin", fin)
    b.add_edge(START, "ask")
    b.add_edge("ask", "fin")
    b.add_edge("fin", END)

    import tempfile, os
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close()
    saver = SqliteSaver.from_conn_string(db.name)
    saver.setup()
    g = b.compile(checkpointer=saver)
    cfg = {"configurable": {"thread_id": "t-h1"}}

    # 首次 invok 触发 interrupt
    first = g.invoke({"messages": []}, config=cfg)
    # Command resume
    resumed = g.invoke(Command(resume={"approved": True}), config=cfg)
    os.unlink(db.name)
    return {"first_out": first.get("out"), "resumed_out": resumed.get("out")}

check("interrupt + Command resume (SqliteSaver checkpointer)", hitl_test)

# astream_events v2
def astream_v2():
    from langgraph.graph import StateGraph, START, END
    class S(dict):
        pass
    def n(s):
        return {"msg": "hello"}
    b = StateGraph(S)
    b.add_node("n", n)
    b.add_edge(START, "n")
    b.add_edge("n", END)
    g = b.compile()
    async def collect():
        out = []
        async for ev in g.astream_events({"msg": "x"}, version="v2"):
            out.append(ev)
            if len(out) > 1:
                break
        return [e.get("event") for e in out]
    return asyncio.run(collect())
check("astream_events version='v2'", astream_v2)

# get_state_history / aget_state_history 存在性
def snapshots():
    from langgraph.graph import StateGraph, START, END
    class S(dict):
        pass
    def n(s):
        return {"msg": "hello"}
    b = StateGraph(S)
    b.add_node("n", n)
    b.add_edge(START, "n")
    b.add_edge("n", END)
    g = b.compile()
    return {
        "get_state": hasattr(type(g), "get_state"),
        "get_state_history": hasattr(type(g), "get_state_history"),
        "aget_state": hasattr(type(g), "aget_state"),
        "aget_state_history": hasattr(type(g), "aget_state_history"),
        "update_state": hasattr(type(g), "update_state"),
    }
check("state/state_history methods", snapshots)

print("\n===== RESULTS =====")
ok = True
for name, status, detail in results:
    print(f"[{status}] {name} -> {detail}")
    if status == "FAIL":
        ok = False
print("\nALL_OK" if ok else "\nHAS_FAILURES")
