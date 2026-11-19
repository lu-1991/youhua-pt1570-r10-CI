# 云编译流水线

`.github/workflows/build.yml` 一共 12 步，全部在 GitHub 的 `ubuntu-24.04` 上跑，
仓库里只有几百 KB 的脚本和补丁，源码与工具链都是**运行时在线拉取**。

| # | 步骤 | 做什么 | 备注 |
| --- | --- | --- | --- |
| 1 | checkout | 取本仓库 | |
| 2 | 读取编译参数 | 从 `config/build.env` 读上游提交/运行时版本/默认机型 | 也可用 `workflow_dispatch` 覆盖 |
| 3 | 释放磁盘 + 装依赖 | 删 dotnet/android/ghc，装 clang-18、rust 依赖等 | 完整构建需要 ~40 GB |
| 4 | 拉上游 PonWRT | `git fetch --depth 1 origin <PIN>` | 不落盘到仓库 |
| 5 | 打适配补丁 | `git apply patches/*.patch` + `sh -n` 语法检查 | 补丁只改一个文件 |
| 6 | 取锁定 OpenWrt | 上游 `port/scripts/prepare.sh`（固定提交 `79bc56cb`） | 上游自己维护的锁 |
| 7 | 准备 OMCI 编译器 | rust 1.96.1 + `aarch64-unknown-linux-musl` | 上游要求 |
| 8 | 缓存 `dl/` | `actions/cache` 键 = `sources.lock.json` + `feeds.conf` 哈希 | 二次构建省 10+ 分钟 |
| 9 | 运行时文件与签名密钥 | 从上游 Release 拉 `tyfza1-runtime-inputs-v1.tar.gz`；FIT 开发密钥用公开测试密钥并校验 SHA-256 | 可用 secret `TYFZA1_FIT_SIGNING_KEY` 覆盖 |
| 10 | 组装 `.config` | 上游 `tyfza1-boot.config` + 本仓库 `config/pt1570-r10.config` | 只覆盖机型相关项 |
| 11 | 编译 | `port/scripts/build.sh sources/openwrt $(nproc)`，日志存 artifact | 4 核约 2.5–5 小时，超时 420 分钟 |
| 12 | 打包 + 契约校验 | `tools/make_bundle.py` → `tools/verify_bundle.py --strict-raw` | 校验失败即红，不出包 |
| 13 | 上传 / 发布 | artifact 保留 30 天；填了 `release_tag` 就打 zip 发 Release | |

## 为什么必须整包编译

上游的内核不是上游 OpenWrt 官方 target，而是 `port/overlay/target/linux/rtl9617c` 里
一整套 RTL9617C 驱动（PON MAC、NI/FE 硬件转发、RTL8852C/8192XB 无线、两个 MIPS PE 核的
固件、LED/PHY 适配）。`build.sh` 还会先交叉编译 PE 核的固件并塞进内核源码树，所以
"只编内核"或"复用官方 image"都拿不到能用的产物。这也是本仓库选择"云端整包编译 +
只把补丁和打包逻辑留在本地"的原因。

## 改上游版本

```bash
# config/build.env
PONWRT_REF=<新的提交号>
```

然后重跑 workflow。若上游改了 `platform.sh` 的结构，补丁会打不上 —— 这一步失败得很快
（第 5 步），不会浪费几小时。处理办法见 [patches/README.md](../patches/README.md)。

## 加软件包

编辑 `config/pt1570-r10.config`，追加 `CONFIG_PACKAGE_xxx=y`。上游 `build.sh` 会把这行
带进 `.config` 并 `make defconfig`；如果包不存在，defconfig 会忽略它（不会报错），
所以打包后到 LuCI 看一眼有没有装上。

体积上限要留意：rootfs 不能超过 33,648,640 B（原厂根卷容量），超了 `verify_bundle.py`
直接失败。需要大空间的话，先删包再加包。

## 本地复现（可选）

只要有 Linux/WSL2：

```bash
git clone <你的仓库> && cd pt1570-r10-CI
git init ponwrt && git -C ponwrt remote add origin https://github.com/LeoooChen/PonWRT-TYFZA1
git -C ponwrt fetch --depth 1 origin "$(grep ^PONWRT_REF config/build.env | cut -d= -f2)"
git -C ponwrt checkout FETCH_HEAD
for p in patches/*.patch; do git -C ponwrt apply "$p"; done
cd ponwrt && sh port/scripts/prepare.sh sources/openwrt
# 之后按 workflow 第 7–11 步设置 TYFZA1_* 环境变量并编译
```

Windows 上不装 WSL 也能做的是：`tools/selftest.py`（合成镜像验证打包/校验逻辑）、
`tools/parse_env.py`、`tools/verify_bundle.py`、`tools/split_dump.py`、`tools/tftp_serve.py`。
