# -*- coding: utf-8 -*-
"""生成蓝色 ¥ 应用图标 fapiao.ico（白底圆角卡片 + 蓝色 ¥，与窗口图标一致）"""
from PIL import Image, ImageDraw, ImageFont

OUT = os.path.join(ROOT, 'buildqt', 'fapiao.ico')
FONT = r'C:\Windows\Fonts\msyhbd.ttc'
S = 256

img = Image.new('RGBA', (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
m = S * 0.05
d.rounded_rectangle([m, m, S - m, S - m], radius=int(S * 0.24),
                    fill=(255, 255, 255, 255),
                    outline=(200, 220, 255, 255), width=int(S * 0.055))
f = ImageFont.truetype(FONT, int(S * 0.76))
bb = d.textbbox((0, 0), '¥', font=f)
w, h = bb[2] - bb[0], bb[3] - bb[1]
d.text(((S - w) / 2 - bb[0], (S - h) / 2 - bb[1] - S * 0.015),
       '¥', font=f, fill=(31, 111, 235, 255))

sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128),
         (256, 256)]
img.save(OUT, format='ICO', sizes=sizes)
print('saved', OUT)
