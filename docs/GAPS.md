# 缺口清单：三根柱子对照最初的规划

> 2026-11-12 快照。规划原文在 `gisel/docs/PLAN-internal-authority.md`；这里只记
> “现在到哪了、还差什么”，不重复论证。数字全部来自 `EVIDENCE.json`
> （`VERIFY_FROZEN=1 ./scripts/verify-evidence.sh` → 76/76）和 `SCOPE.md §7`。

三根柱子：

1. **gisel**：`.gisel` → `inc`；`.gisel` → 无答案查询（`domain`、`cases`）。
2. **gisel-anchor**：从 pin 的上游规则区域出题（`U`、`U-full`）。
3. **ab**：两侧跑同一份查询，证明答案一致（`G` 证明 gisel 侧，`U` 证明上游侧）。

## 1 覆盖矩阵

| 目标 | G（cases） | D（domain） | U（采样） | U-full | MIR L1/L2 | L3 |
|---|---:|---:|---:|---:|:-:|:-:|
| x86 | 3,014 | 91,340 | 10,027 | 965,592 | ✓ | ✓ |
| aarch64 | 12,559 | 8,432,640 | 29,608 | 3,951,279 | ✓ | ✓ |
| riscv | 6,086 | 2,029,976 | 20,445 | 2,022,608 | ✓ | ✓ |
| arm | 1,614 | 9,296 | 15,695 | 1,769,782 | L1+L2 | — |
| mips | 1,647 | 18,745 | 11,396 | 1,264,130 | L1+L2 | — |
| ppc | 94 | 1,005 | 2,177 | 252,826 | L1（名义）+ L2 | — |
| m68k | 35 | 112 | 2,182 | 252,826 | L2 | — |
| wasm | 894 | 11,996 | 9,290 | 1,011,304 | L2 | — |
| spirv | 5,689 | 3,472,080 | 15,592 | 4,082,382 | L2 | — |

九个 target 都有 L0（`getAction`）+ D + U + U-full + 负控；MIR/llc 层覆盖九个：
x86 / aarch64 / riscv 有 L1+L2+L3，arm / mips / ppc 有 L1+L2（PPC 的 L1 名义），
m68k / wasm / spirv 有 L2。
PPC 的 L1 只有一条 `legalize-ret.mir`，文件里没有 generic 指令，负控（G_ADD）
抓不到差异；它的真实覆盖来自 L2：71 条查询生成的 MIR，`G_FADD` 负控能抓到。

## 2 这一轮关掉的

| # | 缺口 | 处置 |
|---|---|---|
| C1 | `gisel` CI 从 2026-11-11 起是红的：`test_packaging_reads_the_package_version` 在 3.11/3.12 上报 `KeyError: 'version'`（3.10 走正则路径，所以本地绿） | `6b1d763` 修测试；顺手把 CI 的 lint / `gen --emit={inc,check,core}` 从 3 个语料扩到 `tests/corpus/*.gisel` |
| C2 | L3 只在日志里、没进 `EVIDENCE.json` | `1edf867`：`l3.py` + `kind: "lit"`，逐条 `(status, expected)`，80,596 条，0 变化 |
| C3 | gisel-anchor 没有 CI | `ada7db3`：`import-tests`（80 passed）与 `import-check`（pin 的 partial clone，7 MB，九目标 `--check` 16 s）两个 job；run `43146080543` 绿 |
| C4 | `graph` 的闭包审计默认不成立 | `gisel f1472b2`：`--check-closure` 改为默认开启（`--no-check-closure` 显式退出）；四个大 target 默认 rc=1，`--refine` 后 rc=0。spec/README/REPORT 同步 |
| C5 | ours 构建在已打补丁的树上跑 `check-pin.sh`，x86/a64/riscv 的 G 路径在内容冻结后失效 | `ab.py` 改为打补丁前校验还原树，`build_swap_runner(..., skip_pin=True)` 只跳过 ours；官方侧校验没有放宽 |
| C6 | 改 `.gisel` 后没有 frozen 回放命令（原 O3） | `replay-frozen.py`：九 target × G/U 共 18 个方向逐条一致；见 `docs/REPLAY-FROZEN.md` |
| C7 | `agree` 把“谁都没答”算成一致 | 对外报 `answered_equal`；U-full 表加列（AArch64 3,475,458 → 3,440,142）；`EVIDENCE.json` 的 28 个答案类条目钉住该字段 |
| C8 | U-full 的展开题面没有冻结，容易被读成丢文件 | `verify-evidence.sh` 在跑 U-full 时打印 NOTE：只冻结 universe + expand 命令，展开文件按需生成 |
| C9 | `run-x86-end-to-end.sh` 在打补丁后建 ours runner，同样被 `check-pin.sh` 拒绝 | ours 的 build 设 `GIT_DSL_SKIP_PIN_CHECK=1`；official build 仍在还原树上校验。实测整条脚本 rc=0、3,014 条 agree |
| C10 | O1：MIR/llc 层只覆盖 7/9，arm/mips 缺 L2，m68k/wasm/spirv 没有 MIR | 2026-11-12 补齐：arm 1,141、mips 1,311、m68k 25、wasm 588、spirv 2,625 条，diff 全 0，负控全部抓到；`VERIFY_FROZEN=1 ./scripts/verify-evidence.sh MIR` 22/22 |
| C11 | O6：pin 漂移没有政策 | `gisel-anchor` 明确“只跟 pin”：`README.md` + `PIN.json` 的 comment 写死四步显式迁移；不建自动同步 job（见该仓库 README「pin 政策」） |
| C12 | 采用分支不存在：L3 只是“换了再还原”，fork 里没有一处真的是 gisel 的规则 | 三个 L4 target 都有采用分支：`explore/{x86,aarch64,riscv}-adopt`（`4b8a026249c2` / `a355d055cf28` / `0feeca6a2b68`），构造函数体换成 `#include "<T>LegalizerInfo.inc"`。每个分支：inc sha 与 `EVIDENCE.json` 绑定值逐位相同；构建出的 CodeGen 库 sha 与 MIR-L3 “swapped” 记录逐位相同；80,596 条 lit 逐条一致；冻结 G/U 回放 x86 3,014/10,027、aarch64 12,559/29,608、riscv 6,086/20,445，disagree 全 0。决定与回滚见 `docs/ADOPT.md` |

## 3 还缺的

按“价值 / 成本”排序。

### O7 一条龙的 vertical slice（已关闭）

`vertical.py --target {riscv,x86,aarch64}` 把 G/U/负控/domain/MIR L1/L2
串成一条命令，每一步都对 `data/<target>-contract.json` 复核。x86 与
aarch64 的 contract 与冻结副本（`evidence/<target>-vertical/`）已提交，
CI 用 `--verify-only --frozen` 在没有构建树和 `/tmp` 的机器上重放。

AArch64 速记：G 12,559 / U 29,608 / domain 8,432,640（双端断言 351,504）/
L1 214 runs / L2 8,576 runs，全部 disagree 0。负控 G_ZEXT 315、G_ICMP 216。

- 旧 `riscv_vertical.py` 保留为薄壳，命令不变。

### O8 U 题面冻结在 universe 修复之前（已刷新）

`oracle ac98717` 修掉了 3-slot opcode 的短 types 组合（ArrayRef 越界），
但一部分 U/U-full 证据停在修复前。2026-11-12 全量刷新：

- **U（采样）**：aarch64 29,608、riscv 20,445、arm 15,695、mips 11,396、
  ppc 2,177、m68k 2,182、wasm 9,290、spirv 15,592。x86 本来就用新工具
  生成（10,027）。除 spirv 15,580 → 15,592 外条数不变，抽样点按新 universe
  重抽；全部 `disagree=0`，八个负控逐条复现。
- **U-full**：x86 965,592、aarch64 3,951,279、riscv 2,022,608（旧表
  1,121,728 / 3,505,608 / 3,028,496 是修复前 universe 的展开）。x86 有
  1,392 条双端同点断言；aarch64 43,452 条双端断言 + 40,014 条“上游断言、
  我方作答”（三类站点，按 `--allow-ref-asserts` 钉死）；riscv 5,400 条
  双端断言，`disagree=0`。其余六个 target 的 U-full universe 早已是当前
  版本，未重跑。

数值与站点见 `docs/EVIDENCE-U.md`；`EVIDENCE.json` 76/76 通过。

### O9 多步 / 动态 legalize 的等价性（后继闭包已证，边界收窄）

2026-11-12 之前，这条只能写“没有证据”。现在有了：`closure.py` 把
“一步答案相同 + 后继相同 + 状态集对后继封闭 ⇒ 整个循环相同”做成可执行检查，
种子分别取 `G`（cases）和 `D`（domain），跑到不动点：

| target | G 闭包 | D 闭包 | 并集 | disagree | stuck |
|---|---:|---:|---:|---:|---:|
| x86 | 3,370 | 92,132 | 94,327 | 0 | 0 |
| aarch64 | 15,386 | 944,069 | 958,751 | 0 | 0 |
| riscv | 6,347 | 254,065 | 260,156 | 0 | 0 |
| arm | 1,625 | 9,296 | — | 0 | 0 |
| mips | 1,656 | 18,745 | — | 0 | 0 |
| ppc | 95 | 1,005 | — | 0 | 0 |
| m68k | 37 | 112 | — | 0 | 0 |
| wasm | 1,044 | 12,020 | — | 0 | 0 |
| spirv | 5,948 | 3,472,512 | — | 0 | 0 |

九个 target 合计：`G` 种子 31,641 + 新增 3,867，`D` 种子 4,795,281 + 新增 8,675。
mips 的 9 条单边断言（`G_LOAD`）——与冻结 G 证据同数；aarch64 的 1,696 条沿用
domain 钉数。
aarch64 的 1,696 条单边断言沿用 domain 的钉数（`--allow-ref-asserts 1696`）；
双端断言数与 domain 证据逐位一致（x86 648、riscv 1,074、aarch64 39,056+6）。
命令、runner/seed sha、运行时间见 `docs/CLOSURE.md`。

仍未覆盖（边界，不是待办）：

- 起点在声明域之外：`s7`、`s31` 这类宽度，非常见的 mem 描述符/立即数。
  **第一步已测量**（`docs/EVIDENCE-O9.md`）：`ab.py outside` 把冻结 `G` 题面的
  形状改写到位宽 7/31/63/65/127 与 lane 3/5/7，x86 5,387 / riscv 12,313 /
  aarch64 36,558，disagree 全 0；aarch64 18 条两侧都拒绝（官方
  `LowLevelType.h:470` 断言，模型按不精确 `divLanes` 拒绝）。仍余：域外
  **后继**（第二步及以后）没有闭包，非常见 mem 描述符/立即数未探。
- `Custom` 动作体是手写 C++，动作名一致不等于里面的类型改写一致。
- MIR 层副作用由 L1/L2/L3 覆盖，那条链的形状仍来自上游测试。
- 分区闭包（`graph --check-closure`）与后继闭包是两个问题：未 refine 时
  X86 272 / AArch64 1,985 / RISC-V 1,607 / Wasm 272 的类内不封闭仍然存在，
  逐条归因见 `gisel/docs/REPORT-closure-why.md`，
  closure 不替它回答。

## 4 按级别的结论

| 级别 | 谁够得上 | 还差 |
|---|---|---|
| L4（内部权威） | x86 / aarch64 / riscv | **refine 后**静态闭包 + frozen 回放 + corpus→inc 绑定 + CI 都齐；多步等价在声明域的后继闭包上已验证（`docs/CLOSURE.md`），域外起点仍是边界（O9） |
| L3 以下 | 其余六个 | 没有 L3；L0/D/U-full/L2 已齐 |

L4 是**分 target 授予**的，不是一个总开关；九个 target 共享的只有 L0/D/U-full。

采用进度：x86 / aarch64 / riscv 都已有采用分支（`explore/<t>-adopt`），证据见 `docs/ADOPT.md`。采用不是 L4 的新级别，它只是把 L4 证据落到 fork 里；参照系的完整性三者不同（只有 x86 树内有冻结参照构造器）。
