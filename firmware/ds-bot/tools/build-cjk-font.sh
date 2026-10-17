#!/usr/bin/env bash
# 生成 ds-bot UI 用的 32px 中文字体（LVGL 子集）。
#
# 为什么需要：LVGL 自带的 CJK 字体只有 14/16px（太小），Montserrat 没有汉字
# （会显示乱码）。所以用 lv_font_conv 从系统字体裁一个子集。
#
# 依赖：Node.js（用 npx 跑 lv_font_conv）
# 字体：macOS 的 Arial Unicode.ttf（单体 TTF；PingFang/Hiragino 是 TTC，转换器不支持）
#
# 用法：./tools/build-cjk-font.sh [字号]
# 改字号或加字：编辑下面的 SYMBOLS，然后重跑。

set -euo pipefail

SIZE="${1:-32}"
OUT_DIR="$(cd "$(dirname "$0")/.." && pwd)/components/uface"
FONT="/System/Library/Fonts/Supplemental/Arial Unicode.ttf"

[ -f "$FONT" ] || { echo "❌ 找不到 $FONT"; exit 1; }

# UI 词汇子集。新增界面文案时把新字补进来再重跑。
SYMBOLS="待机录音中处理播报完成失败需要用户操作成功错误取消结果未确认请说是的话正在连接网络重试确定好的发送微信邮件打开查看有没有重要今天消息联系人同名正文核对已收件箱垃圾删除回复转发设置关于版本电量时间分钟秒每小到下午上午晚安你好谢谢对不起问一下再来次任务指令语音识别文字屏幕显示状态等待暂停继续新建列表内容摘要总结全部部分附件图片文件下载上传登录账号密码手机电脑主机设备断开在线离线服务器超时数未知异常提示对话框按钮返回上一页下关闭开启允许拒绝权限摄像头麦克风扬声器音量静音播放停止开始结束保存加载编辑修改添加移除清空刷新同步备份恢复默认高级安全隐私你们他她它们和与及在是有无不能可以要会做这个那个什么怎样如何为多少哪里谁时候一二三四五六七八九十零百千万亿年月日周星期点前后左右上下大小长短高低新旧快慢开关进出内外中间旁边里面外面主已追问桌宠定时检查确认经"

echo "生成 ${SIZE}px 中文字体 -> $OUT_DIR/dsbot_font_${SIZE}.c"
npx --yes lv_font_conv \
    --font "$FONT" \
    --size "$SIZE" --bpp 4 --format lvgl \
    --lv-font-name "dsbot_font_${SIZE}" \
    --lv-include lvgl.h \
    --range 0x20-0x7F \
    --range 0x2000-0x206F \
    --range 0x3000-0x303F \
    --range 0xFF00-0xFF5F \
    --symbols "$SYMBOLS" \
    -o "$OUT_DIR/dsbot_font_${SIZE}.c"

cat > "$OUT_DIR/dsbot_font_${SIZE}.h" <<EOF
/* ds-bot UI 中文字体（${SIZE}px，子集，由 tools/build-cjk-font.sh 生成） */
#ifndef DSBOT_FONT_${SIZE}_H
#define DSBOT_FONT_${SIZE}_H
#include "lvgl.h"
#ifdef __cplusplus
extern "C" {
#endif
LV_FONT_DECLARE(dsbot_font_${SIZE});
#ifdef __cplusplus
}
#endif
#endif
EOF
echo "✅ 完成：$(ls -lh "$OUT_DIR/dsbot_font_${SIZE}.c" | awk '{print $5}')"
