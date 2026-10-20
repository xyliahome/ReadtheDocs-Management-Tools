# ReadtheDocs-Management-Tools

## doc_audit.py
扫描中英文 RST/Markdown 文档，检查链接、图片、锚点、中英文一致性以及 Sphinx 实际构建问题，并对能够确定正确答案的问题进行自动修复。
🔗 内部链接xxx.html 指向的文档不存在
🖼️ 图片/媒体图片路径错误、文件不存在
⚓ Anchor#xxx 不存在、Anchor 重复或位置错误
🇨🇳🇬🇧 中英文一致性EN/ZH 文件缺失、标题结构不同
🔄 中英文 Anchor 对齐中文标题的 Anchor 和英文对应标题不一致
🌐 跨仓库/外链EnOS 其他产品文档链接或第三方 URL 无法访问
🔢 文档版本同一 repo 的链接里出现不一致的 version segment
📊 表格Sphinx HTML 构建实际报告的 table directive 渲染错误

## clear_empty.sh
清理TOC指向文档内容为空的目录页

## find_orphan_docs.py
报告 TOC 引用了但实际找不到的文件
以 index.rst 为入口，递归解析 .. toctree::，建立实际参与文档目录的 .rst/.md 文件集合，然后和磁盘上实际存在的所有文档进行比较。

## media_cleaner.sh
清理仓库中没用到的图片，删除前会先确认，包括：
.png
.jpg
.jpeg
.gif
.svg

