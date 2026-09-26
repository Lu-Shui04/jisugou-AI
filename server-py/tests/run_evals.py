"""评测脚本：提示词注入防护 + 知识库检索 + 退款意图判定

用法（在 server-py 目录下、或容器内执行）：

    python -m tests.run_evals --suite security      # 防注入评测（会真实调用小模型）
    python -m tests.run_evals --suite refund        # 退款意图判定评测（五分类，真实调用小模型）
    python -m tests.run_evals --suite rag           # 检索召回评测（真实调用 Embedding）
    python -m tests.run_evals --suite all
    python -m tests.run_evals --suite security --use-cache   # 用缓存，省钱

输出内容（面试可直接讲）：
    - 混淆矩阵 / 准确率 / 精确率 / 召回率 / F1
    - 每一层拦下了多少（白名单 / 规则 / 小模型 / 缓存）→ 说明"省了多少模型调用"
    - 平均判定耗时、小模型 token 消耗
    - 漏报（攻击被放行）与误报（正常被拦）的具体样本，方便针对性改规则
    - 检索评测：Recall@K / MRR
"""
import argparse
import asyncio
import json
import os
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EVAL_DIR = os.path.join(BASE_DIR, "evals")


def load_cases(name: str) -> list[dict]:
    path = os.path.join(EVAL_DIR, name)
    with open(path, "r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


async def eval_security(use_cache: bool = False, limit: int = 0) -> dict:
    """防注入评测：默认关闭缓存，指标更诚实"""
    from app.security import guard as g
    from app.security import store

    if not use_cache:
        store.get_cached = lambda text: asyncio.sleep(0, result=None)
        store.set_cached = lambda text, value: asyncio.sleep(0, result=None)

    cases = load_cases("prompt_injection_cases.jsonl")
    if limit:
        cases = cases[:limit]

    tp = tn = fp = fn = 0
    layers: dict[str, int] = {}
    latency_total = 0
    tokens_total = 0
    model_calls = 0
    misses: list[dict] = []

    for case in cases:
        started = time.perf_counter()
        verdict = await g.check(case["text"], route="eval")
        latency_total += int((time.perf_counter() - started) * 1000)
        tokens_total += verdict.model_tokens
        if verdict.layer == "model":
            model_calls += 1
        layers[verdict.layer] = layers.get(verdict.layer, 0) + 1

        expect_attack = case["label"] == "attack"
        got_attack = verdict.blocked
        if expect_attack and got_attack:
            tp += 1
        elif expect_attack and not got_attack:
            fn += 1
            misses.append({"id": case["id"], "type": "漏报", "text": case["text"],
                           "layer": verdict.layer, "expect": case["category"],
                           "category": verdict.category})
        elif not expect_attack and got_attack:
            fp += 1
            misses.append({"id": case["id"], "type": "误报", "text": case["text"],
                           "layer": verdict.layer, "expect": case["category"],
                           "category": verdict.category})
        else:
            tn += 1

    total = len(cases)
    accuracy = (tp + tn) / total if total else 0
    precision = tp / (tp + fp) if (tp + fp) else 0
    recall = tp / (tp + fn) if (tp + fn) else 0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0
    no_model = layers.get("whitelist", 0) + layers.get("rule", 0)

    print("=" * 62)
    print("提示词注入防护评测")
    print("=" * 62)
    print("样本总数        : %d（攻击 %d / 正常 %d）" % (total, tp + fn, tn + fp))
    print("混淆矩阵        : TP=%-3d FN=%-3d FP=%-3d TN=%-3d" % (tp, fn, fp, tn))
    print("准确率 Accuracy : %.1f%%" % (accuracy * 100))
    print("精确率 Precision: %.1f%%   （判为攻击的里面有多少是真攻击 → 看误报）" % (precision * 100))
    print("召回率 Recall   : %.1f%%   （真攻击里拦下了多少 → 看漏报）" % (recall * 100))
    print("F1              : %.1f%%" % (f1 * 100))
    print("-" * 62)
    print("各层拦截分布    : " + "，".join("%s=%d" % (k, v) for k, v in sorted(layers.items())))
    print("零 token 拦下   : %d/%d = %.0f%%（白名单+规则，未调用小模型）"
          % (no_model, total, (no_model / total * 100) if total else 0))
    print("小模型调用      : %d 次，累计 %d token" % (model_calls, tokens_total))
    print("平均判定耗时    : %d ms" % (latency_total // total if total else 0))
    if misses:
        print("-" * 62)
        print("错例明细（改规则就看这里）：")
        for item in misses:
            print("  [%s] %-12s %s" % (item["type"], item["id"], item["text"][:44]))
            print("        层级=%s 判定分类=%s 期望分类=%s" % (item["layer"], item.get("category"), item["expect"]))
    else:
        print("-" * 62)
        print("没有错例：全部样本判定正确")
    print("=" * 62)

    return {"accuracy": accuracy, "precision": precision, "recall": recall, "f1": f1,
            "tp": tp, "fp": fp, "fn": fn, "tn": tn, "layers": layers,
            "model_calls": model_calls, "tokens": tokens_total,
            "avg_latency_ms": (latency_total // total) if total else 0, "misses": misses}


def eval_rag(top_k: int = 4) -> dict:
    """检索评测：只看召回，不调用大模型"""
    from app.chains.rag_chain import retrieve_with_threshold

    cases = load_cases("rag_cases.jsonl")
    hit_at_k = 0
    reciprocal = 0.0
    rows = []

    for case in cases:
        hits = retrieve_with_threshold(case["question"], top_k=top_k)
        sources = [(doc.metadata or {}).get("source", "") for doc, _ in hits]
        rank = 0
        for index, source in enumerate(sources, start=1):
            if source in case["expect_sources"]:
                rank = index
                break
        if rank:
            hit_at_k += 1
            reciprocal += 1.0 / rank
        rows.append((case["question"], rank, sources[:3]))

    total = len(cases)
    recall = hit_at_k / total if total else 0
    mrr = reciprocal / total if total else 0

    print("=" * 62)
    print("知识库检索评测（Top-K=%d，含相似度阈值过滤）" % top_k)
    print("=" * 62)
    print("样本总数        : %d" % total)
    print("命中率 Recall@%d : %d/%d = %.1f%%" % (top_k, hit_at_k, total, recall * 100))
    print("MRR             : %.3f（命中排得越靠前越高，1.0 表示每次都是第一条）" % mrr)
    print("-" * 62)
    for question, rank, sources in rows:
        print("  %s %-26s 排名=%-2s | top1=%s"
              % ("✓" if rank else "✗", question[:26], rank or "-", (sources[0] if sources else "无")[:30]))
    print("=" * 62)
    return {"recall": recall, "mrr": mrr, "total": total, "hits": hit_at_k}


async def eval_refund(use_cache: bool = False, limit: int = 0) -> dict:
    """退款族意图判定评测：动作 / 咨询 / 进度 / 混合 / 无关 五分类

    这是"别写死"的证据：判定不是关键词表，而是规则快路径 + 小模型，这里能量化它准不准。
    默认关闭缓存，避免"第二次必然命中缓存"把指标算虚。
    """
    from app.utils import handoff

    if not use_cache:
        handoff._cache_get = lambda text: asyncio.sleep(0, result=None)
        handoff._cache_set = lambda text, value: asyncio.sleep(0, result=None)

    cases = load_cases("refund_intent_cases.jsonl")
    if limit:
        cases = cases[:limit]

    labels = ["action", "info", "progress", "mixed", "none"]
    matrix = {expect: {got: 0 for got in labels} for expect in labels}
    layers: dict[str, int] = {}
    misses: list[dict] = []
    latency_total = 0
    tokens_total = 0
    model_calls = 0
    correct = 0

    for case in cases:
        started = time.perf_counter()
        verdict = await handoff.classify(case["text"])
        latency_total += int((time.perf_counter() - started) * 1000)
        tokens_total += verdict.get("model_tokens") or 0
        if verdict["layer"] == "model":
            model_calls += 1
        layers[verdict["layer"]] = layers.get(verdict["layer"], 0) + 1

        expect = case["label"]
        got = verdict["kind"]
        matrix[expect][got] = matrix[expect].get(got, 0) + 1
        if expect == got:
            correct += 1
        else:
            misses.append({"id": case["id"], "text": case["text"], "expect": expect, "got": got,
                           "layer": verdict["layer"], "reason": verdict.get("reason")})

    total = len(cases)
    accuracy = correct / total if total else 0

    # 每一类的精确率 / 召回率 / F1（多分类口径）
    stats = {}
    for label in labels:
        tp = matrix[label][label]
        fp = sum(matrix[other][label] for other in labels if other != label)
        fn = sum(matrix[label][other] for other in labels if other != label)
        precision = tp / (tp + fp) if (tp + fp) else 0
        recall = tp / (tp + fn) if (tp + fn) else 0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0
        stats[label] = {"precision": precision, "recall": recall, "f1": f1,
                        "support": tp + fn}

    macro_f1 = sum(item["f1"] for item in stats.values()) / len(labels)
    # 业务上最要命的一种错：把"咨询"当成"要办"（就是把用户的提问堵成"请联系人工"）
    dangerous = sum(matrix[label]["action"] for label in labels if label != "action")
    no_model = layers.get("rule", 0) + layers.get("cache", 0)

    print("=" * 62)
    print("退款族意图判定评测（action / info / progress / mixed / none）")
    print("=" * 62)
    print("样本总数        : %d" % total)
    print("准确率 Accuracy : %d/%d = %.1f%%" % (correct, total, accuracy * 100))
    print("宏平均 F1       : %.3f" % macro_f1)
    print("-" * 62)
    print("分类别指标：")
    for label in labels:
        item = stats[label]
        print("  %-9s 样本=%-3d 精确率=%5.1f%%  召回率=%5.1f%%  F1=%.3f"
              % (label, item["support"], item["precision"] * 100,
                 item["recall"] * 100, item["f1"]))
    print("-" * 62)
    print("判定层分布      : " + "，".join("%s=%d" % (k, v) for k, v in sorted(layers.items())))
    print("零 token 快路径 : %d/%d = %.0f%%（规则命中 + 缓存，没花模型 token）"
          % (no_model, total, (no_model / total * 100) if total else 0))
    print("小模型调用      : %d 次，累计 %d token" % (model_calls, tokens_total))
    print("平均判定耗时    : %d ms" % (latency_total // total if total else 0))
    print("误判成 action   : %d 条（这一类最要命：把用户的提问堵成『请联系人工』）" % dangerous)
    if misses:
        print("-" * 62)
        print("错例明细（改 Prompt / 词表就看这里）：")
        for item in misses:
            print("  [%s] %-34s 期望=%-8s 实际=%-8s 层=%s"
                  % (item["id"], item["text"][:34], item["expect"], item["got"], item["layer"]))
    else:
        print("-" * 62)
        print("没有错例：全部样本判定正确")
    print("=" * 62)

    return {"accuracy": accuracy, "macro_f1": macro_f1, "stats": stats, "matrix": matrix,
            "layers": layers, "model_calls": model_calls, "tokens": tokens_total,
            "misrouted_to_action": dangerous,
            "avg_latency_ms": (latency_total // total) if total else 0, "misses": misses}


def main():
    parser = argparse.ArgumentParser(description="评测脚本：提示词注入防护 / 知识库检索")
    parser.add_argument("--suite", choices=["security", "rag", "refund", "all"], default="all")
    parser.add_argument("--use-cache", action="store_true", help="允许使用判定缓存（更省 token，指标偏乐观）")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条（调试用）")
    parser.add_argument("--top-k", type=int, default=4, help="检索评测的 Top-K")
    args = parser.parse_args()

    if args.suite in ("security", "all"):
        asyncio.run(eval_security(use_cache=args.use_cache, limit=args.limit))
        print()
    if args.suite in ("rag", "all"):
        eval_rag(top_k=args.top_k)
        print()
    if args.suite in ("refund", "all"):
        asyncio.run(eval_refund(use_cache=args.use_cache, limit=args.limit))


if __name__ == "__main__":
    main()
