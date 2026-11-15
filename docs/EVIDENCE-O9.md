# O9 域外起点证据：规则没声明过的类型，第一步两侧同答

日期：2026-11-15。这是 `docs/GAPS.md` §3 O9 里那条边界的第一次测量：
起点落在声明域之外（`s7`、`s31`、`s63`、`s65`、`s127`，奇数 lane），
两侧的第一步 `getAction` 是否一致。

这是一次**探测**，不是 cohort 门禁：题面由本工作台从冻结 `G` 题面的形状改写而来，
不属于 `gisel cases`/`domain` 的输出，也不改变它们的切点。

## 做法

`ab.py outside` 从 `evidence/G/<target>/queries.jsonl.gz` 取形状，把数值换掉：

```sh
cd /home/ubuntu/llvm/gisel-ab
python3 ab.py outside --target x86     --tree /home/ubuntu/llvm/trees/explore \
    --build /home/ubuntu/llvm/builds/explore --out /tmp/o9-x86-full     --limit 200000 --per-base 12
python3 ab.py outside --target riscv   --tree /home/ubuntu/llvm/trees/explore \
    --build /home/ubuntu/llvm/builds/explore --out /tmp/o9-riscv-full   --limit 200000 --per-base 12
python3 ab.py outside --target aarch64 --tree /home/ubuntu/llvm/trees/explore \
    --build /home/ubuntu/llvm/builds/explore --out /tmp/o9-aarch64-full --limit 200000 --per-base 12
```

- 改写保持形状：标量/整数/浮点/指针的 kind、向量的 vector-ness 都不变，只换位宽
  （7/31/63/65/127）和 lane 数（3/5/7）。指针与 IEEE 浮点不动位宽——IR 造不出
  非常规位宽的指针/浮点，探它只是噪音。
- 官方侧是 A/B 用的同一个 runner；我方是 `gisel eval` 在同一份 core 文档上的答案。
- 双方都拒绝记为 `both-unanswerable`；只有一边拒绝是洞，单独计数。

## 输入

| 项 | 值 |
|---|---|
| gisel | `1e6dcdcbd7b05de38e285661f964d6fbb7509954`（修复提交 `a87cd7f`，goldens 重录 `1e6dcdc`） |
| ab | `eef0f518a0b247ef7b3acb61b31cfa4d2587323a` |
| LLVM 树 | `/home/ubuntu/llvm/trees/explore` @ `700e551714ccb7357a191fd41837ff1b4784abf2` |
| LLVM 构建 | `/home/ubuntu/llvm/builds/explore`（`llvm-config 24.0.0git`） |

## 结果

| target | 题面 | agree | both-unanswerable | disagree | 单边拒绝 | 耗时 |
|---|---:|---:|---:|---:|---:|---:|
| X86 | 5,387 | 5,387 | 0 | 0 | 0 | 7.4 s |
| RISC-V | 12,313 | 12,313 | 0 | 0 | 0 | 27.7 s |
| AArch64 | 36,558 | 36,540 | 18 | 0 | 0 | 34.8 s |

AArch64 的 18 条双方都拒绝：题面 `G_VECREDUCE_OR s65,nxv1s65` 与 `s127,nxv1s127`
各 9 条（3 个配置 × 3 个 lane 探针）。官方 runner 在 `LowLevelType.h:470`
（`LLT::divide` 的 `isKnownMultipleOf`）断言，我方按 `divLanes 2 of nxv1s65 is
not exact` 拒绝。两边都没有给出第一步答案，所以不计入 agree，也不计入 disagree。
这不是规则翻译差异：`nxv1s65` 不是任何 IR 能构造的类型。

| target | `queries.jsonl` sha256 | `official.answers.jsonl` | `eval.answers.jsonl` |
|---|---|---|---|
| X86 | `541707f2695a8d5c…` | `abcc3af2769e555d…` | `33c5e43505fade51…` |
| RISC-V | `86718ff449bc2eed…` | `4203c6d4692dc4c4…` | `d5eded28e14ec6f4…` |
| AArch64 | `a893326ebc22035b…` | `a9ad7cc6a9353474…` | `737df1d2f4d87243…` |

## 探测过程中修掉的两个模型缺陷

两边对拍本身不产出结论，它先暴露了 `gisel eval` 的两处“域外就崩”：

1. **形状不是前提**。规则会在没检查的情况下读 `elemOf(slot(i))`，因为声明域里
   那个槽总是向量。域外查询把它喂成标量时，`gisel/llt.py` 的裸 `assert` 抛出
   `AssertionError`，整轮跑挂。现在 `ShapeError` → `unanswerable`（`reason:
   shape: …`），与 C++ 侧 assert 被 `NDEBUG` 编译掉的口径对齐。
2. **通配指针有宽度**。查询里的 `p0:*` 在官方 runner 里先被解析成具体指针宽度
   （x86 `isTarget64BitLP64()`，其余取 DataLayout），谓词永远看到整数；模型保留
   了 `*`，于是 `ssz < minsz` 直接 TypeError（RISC-V `G_ICMP s7,p0:*` 40 条）。
   现在 guard 算术按“表里声明的宽度 → `Subtarget.is64Bit()` → LP64 默认”解析，
   且只在真的比较数字时才解析，`is_scalar` 短路的规则行为不变。

两处都只把“崩溃”变成“答案或 unanswerable”。冻结的答案文件没有变，但 `graph` 的
opaque 计数变了：原先被 `*` 参与整数比较卡住的状态第一次有了答案，五个 golden
与十个 nightly 配置因此重录（`gisel tests/artifacts/graph/`，见 `REPORT-graph.md`）。

## 结论与边界

- 在**声明域之外的第一步**，三个 target 上两侧没有一条规则差异（disagree = 0）。
- 官方侧在这批题面里从不给出“我答不了”：除了上面 18 条断言，全部有明确动作。
  这条环路在域外异常稳健——这是证据的一部分，但口径必须带上：题面由我们生成，
  上限是 `--per-base 12` 覆盖的 12 个探针 × 冻结 `G` 形状，不是“整个类型宇宙”。
- 第二步及以后仍未测量：这份证据只钉第一步。多步等价仍以声明域的后继闭包为准
  （`docs/CLOSURE.md`），域外后继不在其中。
- 域外 `Custom` 动作体、非常见 mem 描述符/立即数仍未覆盖。
