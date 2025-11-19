"""
创建一个简单的应用图标
生成一个带有"监"字的ICO文件
"""
from PIL import Image, ImageDraw, ImageFont
import os


def create_icon():
    """创建应用图标"""
    # 创建256x256的图像
    size = 256
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 绘制背景圆形
    margin = 20
    draw.ellipse(
        [margin, margin, size - margin, size - margin],
        fill='#0078D4',  # Windows蓝色
        outline='#005A9E',
        width=3
    )

    # 添加文字"监"
    try:
        # 尝试使用系统字体
        font = ImageFont.truetype("msyh.ttc", 120)  # 微软雅黑
    except:
        # 如果找不到，使用默认字体
        font = ImageFont.load_default()

    # 绘制文字
    text = "监"
    # 计算文字位置（居中）
    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    x = (size - text_width) / 2
    y = (size - text_height) / 2 - 10

    draw.text((x, y), text, fill='white', font=font)

    # 保存为ICO文件（多种尺寸）
    icon_path = os.path.join(os.path.dirname(__file__), '..', 'app.ico')
    img.save(icon_path, format='ICO', sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])

    print(f"图标已创建: {os.path.abspath(icon_path)}")
    return icon_path


if __name__ == "__main__":
    try:
        create_icon()
    except ImportError:
        print("错误: 需要安装Pillow库")
        print("请运行: pip install Pillow")
    except Exception as e:
        print(f"创建图标失败: {e}")
