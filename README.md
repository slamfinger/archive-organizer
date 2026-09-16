# archive-organizer · 通用归档整理技能

把散乱目录整理成「扁平 + 标签化」档案结构的通用技能套件：文件夹只划层级，文件靠标签压平，不增层。全部脚本不写死路径，归档根用 `--root` 传入，跨项目复用；技能本体不含任何特定项目的关键词或画像——项目数据只存在于各项目自己的 `归档整理/` 工作目录中。

## 安装

复制到 Agent 技能目录（如 `~/.agents/skills/`）即可，无第三方依赖（Python 3.10+，读 PDF 时可选装 `pdftotext` 或 `pypdf`）。

## 八个脚本

| 脚本 | 作用 |
|---|---|
| `ao_scan.py` | 全量扫描生成清单（只读） |
| `ao_schema.py` | 清单 → 骨架评审 CSV（不移动） |
| `ao_deep.py` | 读文档正文辅助定类（只读，最后手段） |
| `ao_semantic.py` | 语义错位探测：从目录树推导域画像，输出候选 CSV 供人审 |
| `ao_verify.py` | 静态核验规则 CSV 与目录现状（只读） |
| `ao_state.py` | 位置快照：人工调整保护 + 新增文件增量检测 |
| `ao_protect.py` | 归档整理目录隐藏 + 锁定（macOS chflags / Windows attrib） |
| `ao_classify.py` | 按规则 CSV 执行移动（默认干跑，日志可回滚） |

## 安全设计

- **先扫描后动手，默认干跑**：`ao_classify.py` 不加 `--execute` 只预览
- **只移动不删除**：撤销靠移动日志 `--undo` 逆序回放；重名自动加 ` (n)` 绝不覆盖
- **人工评审是必经环节**：骨架评审 CSV、错位候选 CSV、规则 CSV 都要人确认
- **人工调整不可回改**：每次执行后刷新位置基线，两次基线间的移位/改名进入保护清单
- **计数闭环**：verify 预计数 = 干跑计数才允许执行，执行后逐条对账

## 工作流

```
首轮    scan → schema → 人审 → 拟规则 → verify → 干跑 → 确认 → execute → 快照 → lock
增量    unlock → diff（人工调整自动保护，新增出清单）→ 拟增量规则 → verify → 干跑 → 确认 → execute → lock
错位    ao_semantic.py 出候选 CSV → 人审 → 翻译成规则 → 同上执行
```

详细方法论（三层判断、关键词卫生、域画像算法、规则顺序语义、开源借鉴）见 [references/method.md](references/method.md)，完整使用说明见 [SKILL.md](SKILL.md)。

## 借鉴

organize（journal+undo）、rclone/Finder（重名序号）、rsync（exclude 机制）、TF 关键词聚合（域画像词频思想）。
