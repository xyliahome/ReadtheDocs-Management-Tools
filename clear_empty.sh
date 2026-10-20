#!/bin/bash

# 清除空白页面脚本
# 功能：遍历所有.rst文件，删除那些仅包含标题但所有链接文件都不存在的空白页面

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 临时文件
TEMP_FILE="/tmp/blank_pages_$$.tmp"
FILE_INDEX="/tmp/file_index_$$.tmp"

# 清理函数
cleanup() {
    rm -f "$TEMP_FILE" "$FILE_INDEX"
}

# 设置退出时清理
trap cleanup EXIT

# 显示欢迎页面
show_welcome() {
    echo -e "${BLUE}=====================================${NC}"
    echo -e "${BLUE}     空白页面清理工具 v1.0${NC}"
    echo -e "${BLUE}=====================================${NC}"
    echo ""
    echo -e "${YELLOW}功能说明：${NC}"
    echo "  · 扫描所有 .rst 和 .md 文件"
    echo "  · 识别仅包含标题和 toctree 但所有链接文件都不存在的空白页面"
    echo ""
    echo -e "${YELLOW}正在建立文件索引...${NC}"
}

# 建立文件索引
build_file_index() {
    # 查找所有 .rst 和 .md 文件，建立文件名到路径的映射
    find . -type f \( -name "*.rst" -o -name "*.md" \) -print0 | while IFS= read -r -d $'\0' file; do
        # 获取不带扩展名的文件名
        basename_no_ext=$(basename "$file" .rst)
        basename_no_ext=$(basename "$basename_no_ext" .md)
        
        # 获取相对路径（不带扩展名）
        relative_path="${file#./}"
        relative_path_no_ext="${relative_path%.rst}"
        relative_path_no_ext="${relative_path_no_ext%.md}"
        
        # 记录多种可能的映射关系
        echo "$basename_no_ext|$file" >> "$FILE_INDEX"
        echo "$relative_path_no_ext|$file" >> "$FILE_INDEX"
        
        # 如果文件在子目录中，也记录子目录/文件名的映射
        if [[ "$file" =~ .*/.*/.* ]]; then
            dir_file=$(echo "$relative_path_no_ext" | sed 's|.*/\([^/]*/[^/]*\)$|\1|')
            if [[ "$dir_file" != "$relative_path_no_ext" ]]; then
                echo "$dir_file|$file" >> "$FILE_INDEX"
            fi
        fi
    done
    
    # 排序并去重
    sort -u "$FILE_INDEX" -o "$FILE_INDEX"
}

# 查找链接文件是否存在
find_link_file() {
    local link="$1"
    local current_file="$2"  # 当前文件路径，用于解析相对路径
    
    # 获取当前文件的目录
    local current_dir=$(dirname "$current_file")
    
    # 处理相对路径链接（以 ./ 开头）
    if [[ "$link" =~ ^\./(.+)$ ]]; then
        local relative_link="${BASH_REMATCH[1]}"
        # 构建相对于当前文件目录的完整路径
        local full_path="$current_dir/$relative_link"
        # 规范化路径（去除多余的 ./ 和 ../）
        full_path=$(realpath -m "$full_path" 2>/dev/null || echo "$full_path")
        # 转换为相对路径（相对于工作目录）
        full_path="${full_path#./}"
        
        # 在文件索引中搜索这个完整路径
        if grep -q "^${full_path}|" "$FILE_INDEX"; then
            return 0
        fi
        
        # 也尝试搜索仅文件名部分
        if grep -q "^${relative_link}|" "$FILE_INDEX"; then
            return 0
        fi
    fi
    
    # 在文件索引中搜索匹配的文件（原有逻辑）
    if grep -q "^${link}|" "$FILE_INDEX"; then
        return 0  # 找到了
    fi
    
    # 尝试更灵活的匹配（处理路径分隔符差异）
    local link_alt=$(echo "$link" | tr '/' '_')
    if grep -q "^${link_alt}|" "$FILE_INDEX"; then
        return 0
    fi
    
    # 反向尝试
    local link_alt2=$(echo "$link" | tr '_' '/')
    if grep -q "^${link_alt2}|" "$FILE_INDEX"; then
        return 0
    fi
    
    return 1  # 没找到
}

# 检查单个文件是否为空白页面
check_blank_page() {
    local file="$1"
    
    # 使用 awk 来检测和提取 toctree 中的链接
    local result=$(awk '
    BEGIN { 
        in_toctree = 0
        found_links = 0
        links = ""
    }
    /^\.\.[ ]+toctree::$/ { 
        in_toctree = 1
        next 
    }
    in_toctree && /^$/ { next }  # 跳过空行
    in_toctree && /^[^[:space:]]/ { 
        in_toctree = 0  # 非缩进行，toctree 结束
    }
    in_toctree && /^[[:space:]]+:/ { 
        next  # 跳过选项行
    }
    in_toctree && /^[[:space:]]+[^:]/ {
        gsub(/^[[:space:]]+/, "", $0)  # 去除前导空格
        
        # 处理带尖括号的链接格式，如 "Create Alert <create_alert>"
        if (match($0, /<([^>]+)>/)) {
            # 提取尖括号内的内容
            link_target = substr($0, RSTART+1, RLENGTH-2)
            gsub(/\.rst$/, "", link_target)  # 去除 .rst 后缀
            gsub(/\.md$/, "", link_target)   # 去除 .md 后缀
            if (link_target != "") {
                found_links++
                if (links == "") {
                    links = link_target
                } else {
                    links = links " " link_target
                }
            }
        } else {
            # 处理普通链接格式
            gsub(/\.rst$/, "", $0)         # 去除 .rst 后缀
            gsub(/\.md$/, "", $0)          # 去除 .md 后缀
            if ($0 != "") {
                found_links++
                if (links == "") {
                    links = $0
                } else {
                    links = links " " $0
                }
            }
        }
    }
    END {
        print found_links "|" links
    }
    ' "$file")
    
    local count=$(echo "$result" | cut -d'|' -f1)
    local links=$(echo "$result" | cut -d'|' -f2)
    
    # 如果没有找到任何链接，不算空白页面
    if [[ "$count" == "0" ]]; then
        return 1
    fi
    
    # 检查所有链接文件是否都不存在
    local all_missing=true
    local existing_links=""
    local missing_links=""
    
    for link in $links; do
        if find_link_file "$link" "$file"; then
            all_missing=false
            if [[ -n "$existing_links" ]]; then
                existing_links="$existing_links $link"
            else
                existing_links="$link"
            fi
        else
            if [[ -n "$missing_links" ]]; then
                missing_links="$missing_links $link"
            else
                missing_links="$link"
            fi
        fi
    done
    
    # 如果所有链接都不存在，则是空白页面
    if $all_missing; then
        # 将结果写入临时文件
        echo "$file|$missing_links" >> "$TEMP_FILE"
        return 0
    fi
    
    return 1
}

# 显示找到的空白页面
show_blank_pages() {
    if [[ ! -f "$TEMP_FILE" || ! -s "$TEMP_FILE" ]]; then
        echo -e "${GREEN}✓ 恭喜！未发现空白页面。${NC}"
        echo ""
        return 1
    fi
    
    local count=$(wc -l < "$TEMP_FILE")
    echo -e "${YELLOW}发现 $count 个空白页面：${NC}"
    echo ""
    
    local i=1
    while IFS='|' read -r file links_info; do
        echo -e "${RED}  $i. $file${NC}"
        echo -e "     缺失的链接文件: ${YELLOW}$links_info${NC}"
        echo ""
        ((i++))
    done < "$TEMP_FILE"
    
    return 0
}

# 确认删除操作
confirm_deletion() {
    echo -e "${YELLOW}是否要删除这些空白页面？ [y/N]${NC}"
    read -r response
    case "$response" in
        [yY]|[yY][eE][sS])
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

# 删除空白页面
delete_blank_pages() {
    local deleted_count=0
    local failed_count=0
    
    echo ""
    echo -e "${YELLOW}正在删除空白页面...${NC}"
    
    while IFS='|' read -r file links_info; do
        if rm -- "$file" 2>/dev/null; then
            echo -e "${GREEN}  ✓ 已删除: $file${NC}"
            ((deleted_count++))
        else
            echo -e "${RED}  ✗ 删除失败: $file${NC}"
            ((failed_count++))
        fi
    done < "$TEMP_FILE"
    
    echo ""
    echo -e "${GREEN}删除完成！共删除 $deleted_count 个文件。${NC}"
    if (( failed_count > 0 )); then
        echo -e "${RED}删除失败 $failed_count 个文件。${NC}"
    fi
}

# 主函数
main() {
    show_welcome
    
    # 建立文件索引
    build_file_index
    
    echo -e "${YELLOW}正在扫描文件...${NC}"
    echo ""
    
    # 扫描所有.rst和.md文件
    find . -type f \( -name "*.rst" -o -name "*.md" \) -print0 | while IFS= read -r -d $'\0' file; do
        check_blank_page "$file"
    done
    
    # 显示结果
    if ! show_blank_pages; then
        exit 0
    fi
    
    # 确认删除
    if confirm_deletion; then
        delete_blank_pages
    else
        echo -e "${BLUE}操作已取消。${NC}"
    fi
    
    echo ""
    echo -e "${BLUE}感谢使用空白页面清理工具！${NC}"
}

# 运行主函数
main