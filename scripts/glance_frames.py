"""Generate registered gaze frames from two eye poses. Requires Pillow."""
from PIL import Image, ImageChops, ImageDraw, ImageFilter


def bake(neutral, edited, eyes):
    """Yield (crop_box, RGBA) frames on a shared canvas.

    Each eye supplies an inner-lid polygon and source/target pupil centres
    measured on the neutral crop's pixel grid. The two source images must
    represent the same head pose and crop.
    """
    width, height = neutral.size
    if edited.width * height != edited.height * width:
        raise ValueError('Generated crop aspect ratio changed')
    source = neutral.convert('RGB')
    target = edited.convert('RGB').resize(source.size, Image.Resampling.LANCZOS)
    masks = []
    for polygon, _, _ in eyes:
        mask = Image.new('L', source.size)
        ImageDraw.Draw(mask).polygon(polygon, fill=255)
        masks.append(mask.filter(ImageFilter.GaussianBlur(1.15)))
    if not masks:
        raise ValueError('At least one eye mask is required')
    union = masks[0]
    for mask in masks[1:]:
        union = ImageChops.lighter(union, mask)
    box = union.getbbox()
    if box is None:
        raise ValueError('Empty eye region')
    for i in range(9):
        progress = i / 8
        frame = Image.new('RGBA', source.size)
        for mask, (_, original, generated) in zip(masks, eyes, strict=True):
            dx, dy = (b-a for a, b in zip(original, generated))
            def shifted(image, amount):
                return image.transform(source.size, Image.Transform.AFFINE,
                    (1, 0, dx*amount, 0, 1, dy*amount), Image.Resampling.BICUBIC)
            # Put both irises at the same location before interpolating their colour.
            rgb = Image.blend(shifted(source, -progress), shifted(target, 1-progress), progress)
            rgb.putalpha(mask)
            frame.alpha_composite(rgb)
        yield box, frame.crop(box)
