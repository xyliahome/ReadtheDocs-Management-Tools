#!/bin/bash

# 设置颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}===============================================${NC}"
echo -e "${BLUE}           媒体文件清理工具 (Bash版)         ${NC}"
echo -e "${BLUE}===============================================${NC}"
echo

# 获取项目根目录
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root_dir="$script_dir"

echo -e "${BLUE}扫描目录：${NC} $root_dir"
echo

# 格式化文件大小
format_size() {
    local size=$1
    if [ $size -ge 1048576 ]; then
        local mb=$(powershell -Command "[math]::Round($size / 1048576, 2)" 2>/dev/null || echo "$(($size / 1048576))")
        echo "${mb}MB"
    elif [ $size -ge 1024 ]; then
        local kb=$(powershell -Command "[math]::Round($size / 1024, 2)" 2>/dev/null || echo "$(($size / 1024))")
        echo "${kb}KB"
    else
        echo "${size}B"
    fi
}

# 创建临时文件
temp_media=$(mktemp)
temp_unused=$(mktemp)

# 清理函数
cleanup() {
    rm -f "$temp_media" "$temp_unused"
}
trap cleanup EXIT

echo -e "${YELLOW}正在扫描媒体文件...${NC}"

# 查找所有media目录中的图片文件
find "$root_dir" -type d -name "media" | while read -r media_dir; do
    find "$media_dir" -type f \( -iname "*.png" -o -iname "*.jpg" -o -iname "*.jpeg" -o -iname "*.gif" -o -iname "*.svg" \) -print0
done | sort -z > "$temp_media"

# 检查文件引用并找出未使用的文件
echo -e "${YELLOW}正在检查文件引用...${NC}"

total_unused_size=0
unused_count=0

while IFS= read -r -d '' file_path; do
    filename=$(basename "$file_path")
    
    # 检查是否被引用
    found=false
    if grep -r -q --include="*.md" --include="*.rst" --include="*.py" --include="*.html" "$filename" "$root_dir" 2>/dev/null; then
        found=true
    fi
    
    if [ "$found" = false ]; then
        # 获取文件大小
        file_size=$(stat -c%s "$file_path" 2>/dev/null || stat -f%z "$file_path" 2>/dev/null || echo 0)
        relative_path=$(realpath --relative-to="$root_dir" "$file_path" 2>/dev/null || echo "${file_path#$root_dir}")
        
        echo "$file_path|$file_size|$relative_path" >> "$temp_unused"
        total_unused_size=$((total_unused_size + file_size))
        unused_count=$((unused_count + 1))
    fi
done < "$temp_media"

echo

if [ "$unused_count" -eq 0 ]; then
    echo -e "${GREEN}✓ 没有发现未使用的媒体文件！${NC}"
else
    echo -e "${RED}发现 $unused_count 个未使用的媒体文件：${NC}"
    echo -e "${RED}总大小：$(format_size $total_unused_size)${NC}"
    echo
    
    # 显示文件列表
    index=1
    while IFS='|' read -r file_path file_size relative_path; do
        formatted_size=$(format_size $file_size)
        echo -e "${YELLOW}$index.${NC} $relative_path ${BLUE}($formatted_size)${NC}"
        index=$((index + 1))
    done < "$temp_unused"
    
    echo
    echo -e "${RED}⚠️  警告：此操作将永久删除上述文件！${NC}"
    echo -e "${RED}⚠️  请确保您已经备份了重要文件！${NC}"
    echo
    
    # 询问确认
    while true; do
        read -p "是否继续删除这些文件？(y/N): " yn
        case $yn in
            [Yy]* ) 
                echo
                echo -e "${YELLOW}开始删除未使用的文件...${NC}"
                echo
                
                deleted_count=0
                failed_count=0
                
                while IFS='|' read -r file_path file_size relative_path; do
                    if rm "$file_path" 2>/dev/null; then
                        echo -e "${GREEN}✓ 已删除：${NC} $relative_path"
                        deleted_count=$((deleted_count + 1))
                    else
                        echo -e "${RED}✗ 删除失败：${NC} $relative_path"
                        failed_count=$((failed_count + 1))
                    fi
                done < "$temp_unused"
                
                echo
                echo -e "${GREEN}删除完成！成功删除 $deleted_count 个文件"
                if [ "$failed_count" -gt 0 ]; then
                    echo -e "${RED}删除失败 $failed_count 个文件${NC}"
                fi
                break
                ;;
            [Nn]* | "" ) 
                echo -e "${YELLOW}操作已取消${NC}"
                break
                ;;
            * ) 
                echo "请输入 y 或 n"
                ;;
        esac
    done
fi

echo
echo -e "${BLUE}程序执行完毕，按任意键退出...${NC}"
read -n 1 -s 