"""
news_pipeline/disclosure_image_generator.py: 開示速報・TOP5 サムネイル自動生成エンジン
=====================================================================================
X (旧Twitter) でインプレッションを最大化するための、1200x675 (16:9) 高解像度サムネイル画像を自動生成。
- PIL (Pillow) + NotoSansCJK-Bold.ttc によるプロ仕様のダークテーマレンダリング
- 単一開示速報カード (build_disclosure_card)
- 本日の開示 TOP5 サマリーカード (build_daily_top5_card)
"""

import os
import io
from typing import Dict, Any, List, Optional
from PIL import Image, ImageDraw, ImageFont

FONT_PATH_BOLD = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
FONT_PATH_REG = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
FALLBACK_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def get_font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    """フォント取得ヘルパー"""
    target = FONT_PATH_BOLD if bold else FONT_PATH_REG
    if os.path.exists(target):
        try:
            return ImageFont.truetype(target, size)
        except Exception:
            pass
    return ImageFont.truetype(FALLBACK_FONT, size)


class DisclosureImageGenerator:
    """X投稿用サムネイル画像自動生成クラス"""

    WIDTH = 1200
    HEIGHT = 675
    BG_COLOR = (13, 17, 23)        # #0D1117 ダークネイビー
    CARD_BG = (22, 27, 34)         # #161B22
    BORDER_COLOR = (48, 54, 61)    # #30363D
    TEXT_WHITE = (240, 246, 252)
    TEXT_MUTED = (139, 148, 158)
    TEXT_GOLD = (245, 158, 11)     # ゴールド

    # イベント別アクセントカラー
    EVENT_COLORS = {
        "大量保有": (231, 76, 60),      # レッド
        "自社株買い": (46, 204, 113),    # エメラルド
        "業績修正": (243, 156, 18),     # オレンジゴールド
        "決算短信": (243, 156, 18),
        "TOB": (155, 89, 182),         # パープル
        "PTS": (52, 152, 219),         # シアンブルー
        "その他": (149, 165, 166),
    }

    @classmethod
    def generate_single_card(
        cls,
        event_type: str,
        name: str,
        symbol: str,
        headline: str,
        reason: str = "",
        tier: str = "TIER1",
        evs_score: float = 0.0,
        win_prob: float = 0.70,
        holding_days: float = 3.0,
        daily_bp: float = 40.0,
    ) -> bytes:
        """
        単一の重要開示速報サムネイルカード (1200x675) を生成し PNG bytes を返却
        """
        img = Image.new("RGB", (cls.WIDTH, cls.HEIGHT), cls.BG_COLOR)
        draw = ImageDraw.Draw(img)

        # 1. 外枠カードを描画
        margin = 40
        card_rect = [margin, margin, cls.WIDTH - margin, cls.HEIGHT - margin]
        draw.rounded_rectangle(card_rect, radius=20, fill=cls.CARD_BG, outline=cls.BORDER_COLOR, width=2)

        # 2. 上部アクセントカラーバー (ネオンライン)
        accent_color = cls.EVENT_COLORS.get(event_type, cls.EVENT_COLORS["その他"])
        draw.rounded_rectangle([margin + 20, margin + 15, margin + 250, margin + 65], radius=10, fill=accent_color)

        badge_font = get_font(26, bold=True)
        draw.text((margin + 35, margin + 23), f"⚡ {event_type} 速報", fill=(255, 255, 255), font=badge_font)

        tier_font = get_font(22, bold=True)
        draw.rounded_rectangle([cls.WIDTH - margin - 220, margin + 15, cls.WIDTH - margin - 20, margin + 65], radius=10, outline=accent_color, width=2)
        draw.text((cls.WIDTH - margin - 200, margin + 25), f"優先度: {tier}", fill=accent_color, font=tier_font)

        # 3. 銘柄名 & 証券コード
        name_font = get_font(52, bold=True)
        symbol_text = f"（{symbol}）" if symbol else ""
        draw.text((margin + 40, margin + 95), f"{name} {symbol_text}", fill=cls.TEXT_WHITE, font=name_font)

        # 4. 要点ヘッドライン
        hl_font = get_font(36, bold=True)
        draw.text((margin + 40, margin + 180), headline[:32], fill=cls.TEXT_GOLD, font=hl_font)

        if reason:
            reason_font = get_font(26, bold=False)
            draw.text((margin + 40, margin + 245), f"📍 {reason[:42]}", fill=cls.TEXT_MUTED, font=reason_font)

        # 5. クオンツ期待値 グリッドカード (4分割)
        grid_y = margin + 315
        grid_h = 160
        box_w = (cls.WIDTH - 2 * margin - 100) // 4

        metrics = [
            ("AI期待値 (EVS)", f"{evs_score:.1f}", cls.TEXT_GOLD),
            ("過去勝率 (実績)", f"{win_prob*100:.0f}%", (46, 204, 113)),
            ("日次資金効率", f"+{daily_bp:.1f} bp/日", (52, 152, 219)),
            ("想定拘束期間", f"{holding_days:.1f} 日", cls.TEXT_WHITE),
        ]

        for i, (label, val, col) in enumerate(metrics):
            bx = margin + 40 + i * (box_w + 15)
            draw.rounded_rectangle([bx, grid_y, bx + box_w, grid_y + grid_h], radius=12, fill=cls.BG_COLOR, outline=cls.BORDER_COLOR, width=1)
            lbl_font = get_font(20, bold=False)
            val_font = get_font(36, bold=True)
            draw.text((bx + 18, grid_y + 25), label, fill=cls.TEXT_MUTED, font=lbl_font)
            draw.text((bx + 18, grid_y + 75), val, fill=col, font=val_font)

        # 6. フッターブランド透かし
        footer_font = get_font(20, bold=False)
        draw.text((margin + 40, cls.HEIGHT - margin - 45), "⚡ Agy Quantitative Intelligence • TDnet / EDINET リアルタイム解析", fill=cls.TEXT_MUTED, font=footer_font)

        out = io.BytesIO()
        img.save(out, format="PNG", quality=95)
        return out.getvalue()

    @classmethod
    def generate_daily_top5_card(
        cls,
        date_str: str,
        top_items: List[Dict[str, Any]],
    ) -> bytes:
        """
        本日の重要開示 TOP5 サマリー画像 (1200x675) を生成
        """
        img = Image.new("RGB", (cls.WIDTH, cls.HEIGHT), cls.BG_COLOR)
        draw = ImageDraw.Draw(img)

        margin = 35
        card_rect = [margin, margin, cls.WIDTH - margin, cls.HEIGHT - margin]
        draw.rounded_rectangle(card_rect, radius=20, fill=cls.CARD_BG, outline=cls.BORDER_COLOR, width=2)

        # タイトル
        title_font = get_font(40, bold=True)
        draw.text((margin + 35, margin + 25), f"📊【本日大引け】AIクオンツが選ぶ「本日の重要開示 TOP5」", fill=cls.TEXT_GOLD, font=title_font)
        date_font = get_font(22, bold=False)
        draw.text((cls.WIDTH - margin - 220, margin + 35), f"日付: {date_str}", fill=cls.TEXT_MUTED, font=date_font)

        # TOP5 リスト描画 (行高さ 80px)
        row_y = margin + 95
        medals = ["🥇 1位", "🥈 2位", "🥉 3位", "4位", "5位"]

        item_font_main = get_font(26, bold=True)
        item_font_sub = get_font(22, bold=False)

        for i, item in enumerate(top_items[:5]):
            medal = medals[i]
            y = row_y + i * 85
            # 背景ストライプ
            bg_strip = (18, 22, 28) if i % 2 == 0 else cls.CARD_BG
            draw.rounded_rectangle([margin + 20, y, cls.WIDTH - margin - 20, y + 75], radius=10, fill=bg_strip, outline=cls.BORDER_COLOR, width=1)

            # メダル・順位
            draw.text((margin + 35, y + 22), medal, fill=cls.TEXT_GOLD if i < 3 else cls.TEXT_WHITE, font=item_font_main)

            # 銘柄名 & コード
            code_name = f"[{item.get('symbol', '----')}] {item.get('name', '')}"
            draw.text((margin + 170, y + 22), code_name[:14], fill=cls.TEXT_WHITE, font=item_font_main)

            # 開示要点
            headline = item.get("headline", "")
            draw.text((margin + 480, y + 24), headline[:24], fill=cls.TEXT_MUTED, font=item_font_sub)

            # EVS / OAS バッジ
            evs = item.get("evs_score", 0.0)
            draw.text((cls.WIDTH - margin - 220, y + 22), f"EVS {evs:.1f}", fill=cls.TEXT_GOLD, font=item_font_main)

        # フッター
        footer_font = get_font(20, bold=False)
        draw.text((margin + 35, cls.HEIGHT - margin - 40), "💡 AI期待値(EVS)・需給逼迫度・資金回転率から厳選。明日の前場寄り付き注目銘柄", fill=cls.TEXT_MUTED, font=footer_font)

        out = io.BytesIO()
        img.save(out, format="PNG", quality=95)
        return out.getvalue()
