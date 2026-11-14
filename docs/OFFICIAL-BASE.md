# 官方侧基线（冻结）

对拍里的“官方侧”只认一个东西：`PIN.json` 的 `official_base`。它不是注释，是 12 个文件的
sha256；`ab/check-pin.sh TREE` 按内容逐个比对，任何一个字节不同就 rc=1 并点名文件。

## 冻结点

| 项 | 值 |
|---|---|
| 树 | `/home/ubuntu/llvm/trees/explore` @ `700e551714cc`（`explore/06-review-fixes`） |
| 构建 | `/home/ubuntu/llvm/builds/explore`（RelWithDebInfo + assertions） |
| 语料 pin | `9bfc850b9940566bef17a34601826775349ba0e6` |
| 声明 patch | `#237753 #238306 #238277 #235976 #237650 #239136` |
| 内容清单 | `PIN.json` → `official_base.files` |

“官方侧”指官方库里的 `AArch64LegalizerInfo` 等构造函数；两侧读同一份 JSONL，比
`(action, type_idx, new_type)`。ours 侧只把生成的 `.inc` 编进同一个类。

## 和 pin 的三条偏离（都在 `official_base.deviations` 里点名）

| 文件 | 引入 | 会不会改官方答案 |
|---|---|---|
| `AArch64LegalizerInfo.cpp` 的 `numElements()` 硬化 | `535d3873` | **会**。标量类型上按 release 语义返回 0，而不是断言。规则文本没动 |
| `LegalityPredicates.cpp` 的 `memSizeInBytesNotPow2` | `85a4da33` | 不会。这个谓词在本树没有任何 target 引用 |
| `LegalizerInfo.h` 的 `core*` builders | `3f2331a1..700e551` | 不会。只加方法、不加数据成员，上游构造函数不调用；存在的原因是生成的 `.inc` 需要它 |

换句话说：官方库的答案 = pin + 6 patch + **一条**有效的本地调整（AArch64 标量元素计数）。
这条调整让 assertions 构建在标量查询上给出 release 构建会给出的答案，而不是 abort。

## 生成的 `.inc` 不再依赖 `core*`

生成构造器过去会调用 `coreMinScalarSameAs` 这类 `core*` builder；它们只在 fork 的
`LegalizerInfo.h` 里，所以纯 pin 的头文件编不过 ours。现在 gisel 只发公共 builder
（`minScalarOrEltIf`、`widenScalarToNextPow2`、`widenScalarIf`、`bitcastIf` …），
三份产物都能对**纯 pin 头**编译通过。这条不是注释，是门：

```sh
gisel-ab/scripts/inc/check-pin-header.sh \
  --tree /home/ubuntu/llvm/trees/pin --build /home/ubuntu/llvm/builds/pin --self-test
```

它按 `targets/*.json` 逐目标生成 `.inc`，用 `src/gisel_ab/cpp/*_generated_ctor.cpp`
对 pin-only 树做 `-fsyntax-only`；`--self-test` 再往产物里塞一个 `coreLegalIf` 调用，
要求编译失败——否则说明拿错头文件、上面三行是假的。

`official_base` 仍冻在 `700e5517`（那棵树带 `core*`），因为换基线要重跑全部证据；
但它已经不是产物编译的前提。`pin/make-base.sh` 随时可重建纯 pin 交叉检查树。

## 改基线的成本

`official_base.files` 里任何一条变了，就是换基线：重建官方侧、重跑每个方向的证据、
更新 `EVIDENCE.json` 和本文档的冻结点，同一次提交里完成。历史教训：之前 `check-pin.sh`
只查祖先关系，fork 上未声明的改动（AArch64 的 `numElements()` 硬化）混进了官方侧，检查
仍然报 ok；现在按内容校验，会直接点名。

## 复现

证据也钉在同一个构建上：`EVIDENCE.json` 的 `official_build` 记录 9 个
`libLLVM*CodeGen.so` 的 sha256，`verify-evidence.sh` 会逐个校验；构建被重编就
直接 FAIL，逼你重跑而不是继承旧结论。

```sh
ab/check-pin.sh /home/ubuntu/llvm/trees/explore      # 应 rc=0 且打印冻结点
python3 ../gisel-anchor/scripts/import-cxx.py --all \
  --pin ../gisel-anchor/pin/PIN.json --git-repo /home/ubuntu/llvm/llvm-project --check
```
