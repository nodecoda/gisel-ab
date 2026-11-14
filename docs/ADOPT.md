# ADOPT：把三个 target 的规则换成 gisel 生成的 inc

> 2026-11-12。上游 LLVM 树不是 gisel 的仓库；这是“采用”，不是 gisel 的功能。
> 证据口径见 `PLAN-internal-authority.md` §7、§9。**分 target 采用**，不是一个总开关。

> **2026-11-14（r28）**：gisel 的 `.inc` 改成只发上游公共 builder，三份产物的字节都变了
> （不再调用 `core*`）。下面这张表是**历史实验**：分支里提交的仍是它当时测的那份 inc，
> `EVIDENCE.json` 的 adopt 条目因此标了 `tracks_artifact: false`——`adopt-check.sh`
> 仍然校验分支字节，但不再要求它等于当前产物。重做采用（重建 + lit + 回放）是另一次运行，
> 不在 r28 内。新的 `.inc` 已对纯 pin 头编译通过，见 `gisel-ab/scripts/inc/check-pin-header.sh`。

## 1 三个采用点

| target | 分支 | commit | 构造函数改动 | inc sha256 | 采用后库 sha256 |
|---|---|---|---:|---|---|
| x86 | `explore/x86-adopt` | `4b8a026249c2` | −618 行 | `38b278dd…` | `085928fa…` |
| aarch64 | `explore/aarch64-adopt` | `a355d055cf28` | −1,542 行 | `0f6dcf0a…` | `123cd6d8…` |
| riscv | `explore/riscv-adopt` | `0feeca6a2b68` | −809 行 | `fe6427bd…` | `d44d873e…` |

三个分支都从同一个父提交 `700e551714cc`（pin 基线 `explore/06-review-fixes`）分出，
互不依赖；仓库 `github.com/nodecoda/llvm-project`。每个分支一个 commit，只改构造
函数体和新增一个 `.inc`，`legalizeCustom` 及其后代码不动。

完整 sha：

```text
x86     inc 38b278dd2a10fe42fa699b3661fbee575657bf8e9cdb1399a0d031a7c05b1442
        lib 085928fa26a5e772ff250f045ad4874cd5e8cbbc8046fbeeb2406f7394a9c432
aarch64 inc 0f6dcf0a6b91f105fdea64dcc8f18741f50b2e39fcc26b257cf9ad47ec5e669b
        lib 123cd6d80d8df1cbb9cf695f4cb27b9ca5d0fbbe12192fe8d205dece1f0fc2dd
riscv   inc fe6427bdf480143c3ec1d5b35ebba2bda637cce650d83db4f44e4cdcec520e06
        lib d44d873e01080f3007ecd57a52099fe54c6c43af9de12a8d3fd48d2b42bfff16
```

## 2 生成物来自哪里

```sh
cd /home/ubuntu/llvm/gisel
python3 -m gisel gen tests/corpus/<T>LegalizerInfo.gisel --emit=inc --out <T>LegalizerInfo.inc
```

- gisel：`e6811cd`
- corpus sha256：x86 `934a9356…`、aarch64 `704183ba…`、riscv `3293f261…`
- 每份 inc 的 sha 都与 `gisel/tests/artifacts/<t>.inc`、以及 `ab/EVIDENCE.json` 里
  对应 `snapshot.inc_sha256` 逐位相同。fork 编译的就是证据绑定的那一份。

## 3 验证（每个分支都跑同样的三项）

```text
1) 构建 sha   采用分支编出的 <T>CodeGen 库 == MIR-L3 记录里 libs.swapped.<t>
2) 全量回归   llvm-lit 一轮，80,596 条逐条与官方基线比 (status, expected)
3) 冻结回放   evidence/{G,U}/<t> 的官方答案流上重放，flip 必须为 0
```

| target | 库 sha 与 L3 swapped 一致 | lit 逐条 | G 回放 | U 回放 |
|---|---|---|---|---|
| x86 | `085928fa…` ✓ | 80,596 条 0 变化 | 3,014 / disagree 0 | 10,027 / disagree 0 |
| aarch64 | `123cd6d8…` ✓ | 80,596 条 0 变化 | 12,559 / disagree 0 | 29,608 / disagree 0 |
| riscv | `d44d873e…` ✓ | 80,596 条 0 变化 | 6,086 / disagree 0 | 20,445 / disagree 0 |

“逐条”指 `only_in_baseline`、`only_in_combined`、`changed` 三个集合都为空，不是
“两边 pass 数一样”。两侧头条都是 80,655 discovered / 64,916 passed / 15,534
unsupported / 146 xfail。

回放用的是采用分支的 runner（链接采用后的库）：

```text
x86     /tmp/x86-adopt/runner.shipping   sha256 f339cabd…
aarch64 /tmp/a64-adopt/runner/runner.official
riscv   /tmp/riscv-adopt/runner/runner.official
```

## 4 三个待定项的决定

| 问题 | 决定 | 理由 |
|---|---|---|
| inc 是否入库 | **入库**，与 `.cpp` 同一个 commit | 采用分支要能独立构建；生成物就是输入。sha 与 gisel 产物绑定，不会被悄悄重新生成 |
| 由谁生成 | **人工**，命令写进 commit message 与本文件；CI 侧只有 gisel 仓库的 `verify-evidence.sh` 会从 corpus 重生成并比对 | 采用分支不在 LLVM 项目 CI 里；加生成 job 等于把 gisel 塞进上游构建。漂移检测靠一条 sha 对比 |
| 怎么回滚 | `git revert <commit>`（每个分支一个 commit） | 无构建系统改动、无其它文件依赖该 inc |

一键漂移检测（会 fetch 三个分支，需要网络）：

```sh
cd /home/ubuntu/llvm/gisel-ab && ./scripts/adopt-check.sh
```

它比较分支内 `.inc` 的 sha 与 `gisel/tests/artifacts/<t>.inc`，并核对
`EVIDENCE.json` 的 `adopt/*` 条目；三个 target 各自独立，失败不会遮住另一个。

手工检测（改规则前后都跑）：

```sh
sha256sum llvm/lib/Target/<T>/GISel/<T>LegalizerInfo.inc \
          /home/ubuntu/llvm/gisel/tests/artifacts/<t>.inc
```

两个 sha 不同 = fork 里的规则和 gisel 产物不是一份：要么是有意的采用更新（要同步
更新 gisel 仓库），要么有人手改了生成物（必须拒绝）。

## 5 参照系：三个 target 不一样，必须说清楚

- **x86**：树内有冻结参照 `llvm/unittests/Target/X86/X86LegalizerInfoReference.cpp`
  （未随采用改变），编译出的参照 runner 为 `/tmp/runner-x86-reference`
  （sha256 `6b2b6af0a02058a4ec5a4c4630ddadee0b822da9b0df44e04524e5934254f619`）。
  规则再变时，官方答案还能现场问出来。
- **aarch64 / riscv**：树内没有对应的参照构造器。采用之后，官方答案只剩两条路：
  `ab/evidence/{G,U}/<t>` 的冻结答案流（这就是回放用的参照），或 `git revert` 后重建。
  这是已知差异，不是“参照齐全”。

## 6 采用之后怎么改规则

照 `PLAN-internal-authority.md` §9：改 `.gisel` → 重生成 inc → 对 frozen oracle
跑 G/U + 负控（`replay-frozen.py <t> --direction both`）→ 通过才进 fork。
三个分支各自独立；不要在一个分支上同时改两个 target，会把归因搅在一起。

## 7 没证明什么

- 这是三个 target 的采用，不是“全部 target 已切换”；其余六个仍是官方 C++。
- 动态 `legalize` 的多步等价仍未证明（`GAPS.md` O9）；静态闭包结论仍带
  “refine 后”前缀。
- 采用只换规则表；loop、custom action、memdesc、`verify` 都还是上游代码。
- lit 的 80,596 条是上游测试的形状，不是任意 IR；它对“规则翻译”之外的语义
  退化不敏感。
