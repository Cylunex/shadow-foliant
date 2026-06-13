#!/bin/bash
# WebDAV (CloudDrive) 助手脚本 - rclone 版本
# http://127.0.0.1:19798/dav/
# ============================================

RCLONE_REMOTE="clouddrive"
RCLONE="$HOME/.local/bin/rclone"

# 确保 rclone 可用
if [ ! -x "$RCLONE" ]; then
    echo "❌ rclone 未安装，请先运行安装命令：" >&2
    echo "   curl -sSL https://downloads.rclone.org/rclone-current-linux-amd64.zip > /tmp/rclone.zip" >&2
    echo "   cd /tmp && unzip -qo rclone.zip && cp rclone-*-linux-amd64/rclone ~/.local/bin/ && chmod +x ~/.local/bin/rclone" >&2
    exit 1
fi

usage() {
    cat <<EOF
用法:
  $0 ls [path]              # 列出目录（详细）
  $0 lsd [path]             # 仅列子目录
  $0 tree [path]            # 递归树形展示
  $0 get <远程路径> [本地路径] # 下载文件/目录
  $0 put <本地路径> <远程路径> # 上传文件/目录
  $0 copy <远程来源> <远程目标> # 服务端复制
  $0 mv <远程来源> <远程目标>  # 移动/重命名
  $0 rm <远程路径>           # 删除文件
  $0 rmdir <远程目录路径>     # 删除空目录
  $0 purge <远程目录路径>     # 删除目录及其内容
  $0 mkdir <远程目录路径>     # 创建目录
  $0 cat <远程文件路径>       # 查看文件内容
  $0 md5sum <远程文件路径>    # 计算文件的 MD5
  $0 size [path]            # 统计目录大小
  $0 sync <本地路径> <远程路径> # 单向同步（本地→远程）
  $0 bisync <本地路径> <远程路径> # 双向同步
  $0 info                   # 显示连接信息
  $0 conf                   # 打开 rclone 配置

路径示例: /docker/OpenClaw/ 或 /media/Moive/
注意: 路径前不需要加 remote 名，脚本自动拼接
EOF
}

# 把用户输入的 WebDAV 路径转为 rclone remote 路径
remotepath() {
    local path="${1:-/}"
    # 去掉首尾空格，确保以 / 开头
    path="/${path#/}"
    echo "${RCLONE_REMOTE}:${path}"
}

cmd_ls() {
    local path="${1:-/}"
    echo "📂 $path"
    $RCLONE ls "$(remotepath "$path")"
}

cmd_lsd() {
    local path="${1:-/}"
    echo "📂 $path"
    $RCLONE lsd "$(remotepath "$path")"
}

cmd_tree() {
    local path="${1:-/}"
    $RCLONE tree "$(remotepath "$path")"
}

cmd_get() {
    local remote="$1"
    local localpath="${2:-.}"
    if [[ -z "$remote" ]]; then
        echo "用法: $0 get <远程路径> [本地路径]" >&2
        exit 1
    fi
    $RCLONE copy "$(remotepath "$remote")" "$localpath" -v
}

cmd_put() {
    local localpath="$1"
    local remote="$2"
    if [[ -z "$localpath" || -z "$remote" ]]; then
        echo "用法: $0 put <本地路径> <远程路径>" >&2
        exit 1
    fi
    $RCLONE copy "$localpath" "$(remotepath "$remote")" -v
}

cmd_copy() {
    local src="$1"
    local dst="$2"
    if [[ -z "$src" || -z "$dst" ]]; then
        echo "用法: $0 copy <远程来源> <远程目标>" >&2
        exit 1
    fi
    $RCLONE sync "$(remotepath "$src")" "$(remotepath "$dst")" -v
}

cmd_mv() {
    local src="$1"
    local dst="$2"
    if [[ -z "$src" || -z "$dst" ]]; then
        echo "用法: $0 mv <远程来源> <远程目标>" >&2
        exit 1
    fi
    $RCLONE moveto "$(remotepath "$src")" "$(remotepath "$dst")" -v
}

cmd_rm() {
    local path="$1"
    if [[ -z "$path" ]]; then
        echo "用法: $0 rm <远程路径>" >&2
        exit 1
    fi
    $RCLONE delete "$(remotepath "$path")" -v
}

cmd_rmdir() {
    local path="$1"
    if [[ -z "$path" ]]; then
        echo "用法: $0 rmdir <远程目录路径>" >&2
        exit 1
    fi
    $RCLONE rmdir "$(remotepath "$path")" -v
}

cmd_purge() {
    local path="$1"
    if [[ -z "$path" ]]; then
        echo "用法: $0 purge <远程目录路径>" >&2
        exit 1
    fi
    $RCLONE purge "$(remotepath "$path")" -v
}

cmd_mkdir() {
    local path="$1"
    if [[ -z "$path" ]]; then
        echo "用法: $0 mkdir <远程目录路径>" >&2
        exit 1
    fi
    $RCLONE mkdir "$(remotepath "$path")" -v
}

cmd_cat() {
    local path="$1"
    if [[ -z "$path" ]]; then
        echo "用法: $0 cat <远程文件路径>" >&2
        exit 1
    fi
    $RCLONE cat "$(remotepath "$path")"
}

cmd_md5sum() {
    local path="$1"
    if [[ -z "$path" ]]; then
        echo "用法: $0 md5sum <远程文件路径>" >&2
        exit 1
    fi
    $RCLONE md5sum "$(remotepath "$path")"
}

cmd_size() {
    local path="${1:-/}"
    $RCLONE size "$(remotepath "$path")"
}

cmd_sync() {
    local localpath="$1"
    local remote="$2"
    if [[ -z "$localpath" || -z "$remote" ]]; then
        echo "用法: $0 sync <本地路径> <远程路径>" >&2
        exit 1
    fi
    $RCLONE sync "$localpath" "$(remotepath "$remote")" -v --progress
}

cmd_bisync() {
    local localpath="$1"
    local remote="$2"
    if [[ -z "$localpath" || -z "$remote" ]]; then
        echo "用法: $0 bisync <本地路径> <远程路径>" >&2
        exit 1
    fi
    $RCLONE bisync "$localpath" "$(remotepath "$remote")" -v --progress
}

cmd_info() {
    echo "=== CloudDrive WebDAV ==="
    echo "后端:     rclone v$($RCLONE version 2>/dev/null | head -1 | awk '{print $2}')"
    echo "URL:      http://127.0.0.1:19798/dav"
    echo "用户:     chenyxgm@gmail.com"
    echo "服务:     CloudDrive (http://127.0.0.1:19798)"
    echo ""
    echo "目录结构:"
    $RCLONE lsd "$RCLONE_REMOTE:/" 2>/dev/null
}

case "${1:-help}" in
    ls|list)       shift; cmd_ls "$@" ;;
    lsd|dirs)      shift; cmd_lsd "$@" ;;
    tree)          shift; cmd_tree "$@" ;;
    get|download)  shift; cmd_get "$@" ;;
    put|upload)    shift; cmd_put "$@" ;;
    copy)          shift; cmd_copy "$@" ;;
    mv|move|rename) shift; cmd_mv "$@" ;;
    rm|del|delete) shift; cmd_rm "$@" ;;
    rmdir)         shift; cmd_rmdir "$@" ;;
    purge)         shift; cmd_purge "$@" ;;
    mkdir)         shift; cmd_mkdir "$@" ;;
    cat|view)      shift; cmd_cat "$@" ;;
    md5sum)        shift; cmd_md5sum "$@" ;;
    size)          shift; cmd_size "$@" ;;
    sync)          shift; cmd_sync "$@" ;;
    bisync)        shift; cmd_bisync "$@" ;;
    info|status)   cmd_info ;;
    conf|config)   $RCLONE config ;;
    help|--help|-h) usage ;;
    *)             echo "未知命令: $1"; usage; exit 1 ;;
esac
