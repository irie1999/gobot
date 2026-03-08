class_name SpriteGenerator
# =============================================================
#  プロシージャル（自動生成）スプライト
#  SW x SH = 64 x 80 px (キャラクター)
#  IW x IH = 40 x 40 px (カードアイコン)
#  生成結果はキャッシュされ、再生成を避ける
# =============================================================

const SW = 64
const SH = 80
const IW = 40
const IH = 40

static var _cache: Dictionary = {}

# ===================== PUBLIC API =====================

static func get_player() -> ImageTexture:
	if "player" not in _cache:
		_cache["player"] = _make_player()
	return _cache["player"]

static func get_enemy(etype: String) -> ImageTexture:
	if etype not in _cache:
		match etype:
			"villager":    _cache[etype] = _make_villager()
			"soldier":     _cache[etype] = _make_soldier()
			"knight":      _cache[etype] = _make_knight()
			"holy_knight": _cache[etype] = _make_holy_knight()
			_:             _cache[etype] = _make_villager()
	return _cache[etype]

static func get_card_icon(ctype: String) -> ImageTexture:
	var key: String = "icon_" + ctype
	if key not in _cache:
		match ctype:
			"attack":  _cache[key] = _make_icon_attack()
			"defense": _cache[key] = _make_icon_defense()
			"skill":   _cache[key] = _make_icon_skill()
			"power":   _cache[key] = _make_icon_power()
			_:         _cache[key] = _make_icon_attack()
	return _cache[key]

# ===================== CHARACTERS =====================

# 怪物（プレイヤー）- 紫の悪魔型モンスター
static func _make_player() -> ImageTexture:
	var img = _blank(SW, SH)
	# 角（左）
	_rect(img, 18,  2,  7, 20, Color(0.12, 0.00, 0.20))
	_rect(img, 19,  2,  5,  8, Color(0.08, 0.00, 0.14))
	# 角（右）
	_rect(img, 39,  2,  7, 20, Color(0.12, 0.00, 0.20))
	_rect(img, 40,  2,  5,  8, Color(0.08, 0.00, 0.14))
	# 頭
	_circle(img, 32, 26, 17, Color(0.22, 0.00, 0.32))
	# 目（赤い輝き）
	_circle(img, 25, 23,  5, Color(0.90, 0.05, 0.05))
	_circle(img, 39, 23,  5, Color(0.90, 0.05, 0.05))
	# 瞳（オレンジ）
	_circle(img, 25, 23,  2, Color(1.00, 0.55, 0.00))
	_circle(img, 39, 23,  2, Color(1.00, 0.55, 0.00))
	# 口
	_rect(img, 26, 35, 12,  3, Color(0.10, 0.00, 0.15))
	# 牙（白）
	_rect(img, 28, 38,  3,  7, Color(0.92, 0.92, 0.92))
	_rect(img, 33, 38,  3,  7, Color(0.92, 0.92, 0.92))
	# 胴体
	_ellipse(img, 32, 58, 19, 18, Color(0.18, 0.00, 0.28))
	# 左腕
	_rect(img,  6, 44, 12, 20, Color(0.18, 0.00, 0.28))
	# 右腕
	_rect(img, 46, 44, 12, 20, Color(0.18, 0.00, 0.28))
	# 左爪（3本）
	_rect(img,  3, 62,  4, 10, Color(0.08, 0.00, 0.14))
	_rect(img,  8, 64,  3,  8, Color(0.08, 0.00, 0.14))
	_rect(img, 12, 63,  3,  9, Color(0.08, 0.00, 0.14))
	# 右爪（3本）
	_rect(img, 46, 63,  3,  9, Color(0.08, 0.00, 0.14))
	_rect(img, 53, 64,  3,  8, Color(0.08, 0.00, 0.14))
	_rect(img, 57, 62,  4, 10, Color(0.08, 0.00, 0.14))
	# 脚
	_rect(img, 22, 74,  9,  6, Color(0.14, 0.00, 0.20))
	_rect(img, 33, 74,  9,  6, Color(0.14, 0.00, 0.20))
	return ImageTexture.create_from_image(img)

# 村人 - 松明を持った農民
static func _make_villager() -> ImageTexture:
	var img = _blank(SW, SH)
	# 髪
	_circle(img, 32, 13, 12, Color(0.55, 0.35, 0.15))
	# 顔
	_circle(img, 32, 17, 10, Color(0.95, 0.78, 0.62))
	# 目
	_circle(img, 28, 15,  2, Color(0.20, 0.15, 0.10))
	_circle(img, 36, 15,  2, Color(0.20, 0.15, 0.10))
	# 口
	_rect(img, 29, 22,  6,  2, Color(0.70, 0.40, 0.35))
	# 首
	_rect(img, 28, 26,  8,  5, Color(0.90, 0.75, 0.60))
	# 服（茶）
	_rect(img, 17, 30, 30, 26, Color(0.55, 0.32, 0.12))
	# ベルト
	_rect(img, 17, 53, 30,  4, Color(0.32, 0.18, 0.06))
	# ズボン
	_rect(img, 17, 57, 30, 18, Color(0.42, 0.36, 0.24))
	_rect(img, 30, 57,  4, 18, Color(0.34, 0.28, 0.18))
	# 靴
	_ellipse(img, 23, 76,  9,  4, Color(0.28, 0.18, 0.08))
	_ellipse(img, 41, 76,  9,  4, Color(0.28, 0.18, 0.08))
	# 左腕
	_rect(img,  7, 30, 10, 22, Color(0.55, 0.32, 0.12))
	_circle(img, 12, 53,  5, Color(0.90, 0.75, 0.60))
	# 右腕
	_rect(img, 47, 30, 10, 22, Color(0.55, 0.32, 0.12))
	_circle(img, 52, 53,  5, Color(0.90, 0.75, 0.60))
	# 松明（棒）
	_rect(img, 55,  8,  3, 46, Color(0.60, 0.42, 0.18))
	# 炎
	_circle(img, 56,  8,  6, Color(1.00, 0.65, 0.05))
	_circle(img, 56,  7,  4, Color(1.00, 0.90, 0.30))
	_circle(img, 55,  5,  2, Color(1.00, 1.00, 0.70))
	return ImageTexture.create_from_image(img)

# 兵士 - 鎧と剣盾の戦士
static func _make_soldier() -> ImageTexture:
	var img = _blank(SW, SH)
	# 兜
	_ellipse(img, 32, 12, 13, 11, Color(0.42, 0.42, 0.52))
	_rect(img, 20, 12, 24, 12, Color(0.42, 0.42, 0.52))
	# バイザー（暗）
	_rect(img, 24, 17, 16,  5, Color(0.18, 0.18, 0.28))
	# 顔
	_rect(img, 24, 22, 16,  8, Color(0.88, 0.72, 0.58))
	_circle(img, 29, 25,  2, Color(0.22, 0.22, 0.38))
	_circle(img, 35, 25,  2, Color(0.22, 0.22, 0.38))
	# 胴鎧
	_rect(img, 15, 30, 34, 28, Color(0.45, 0.45, 0.55))
	_rect(img, 23, 34, 18, 18, Color(0.36, 0.36, 0.48))
	# ベルト
	_rect(img, 15, 56, 34,  5, Color(0.32, 0.26, 0.14))
	# 脚甲
	_rect(img, 17, 61, 13, 19, Color(0.42, 0.42, 0.52))
	_rect(img, 34, 61, 13, 19, Color(0.42, 0.42, 0.52))
	# ブーツ
	_ellipse(img, 23, 80, 10,  4, Color(0.24, 0.20, 0.14))
	_ellipse(img, 41, 80, 10,  4, Color(0.24, 0.20, 0.14))
	# 左腕＋青盾
	_rect(img,  3, 30, 12, 26, Color(0.42, 0.42, 0.52))
	_rect(img,  1, 28, 14, 32, Color(0.30, 0.30, 0.72))
	_rect(img,  4, 32,  8,  4, Color(0.85, 0.72, 0.20))
	_rect(img,  8, 28,  2, 32, Color(0.24, 0.24, 0.62))
	# 右腕＋剣
	_rect(img, 49, 30, 12, 26, Color(0.42, 0.42, 0.52))
	_rect(img, 56,  4,  4, 38, Color(0.78, 0.80, 0.88))
	_rect(img, 50, 36, 14,  4, Color(0.65, 0.55, 0.20))
	return ImageTexture.create_from_image(img)

# 騎士 - 重装甲・赤い羽根飾り
static func _make_knight() -> ImageTexture:
	var img = _blank(SW, SH)
	# 羽根飾り（赤）
	_rect(img, 29,  0,  6, 13, Color(0.80, 0.10, 0.10))
	# 兜（フルプレート）
	_ellipse(img, 32, 13, 14, 12, Color(0.58, 0.60, 0.66))
	_rect(img, 19, 13, 26, 16, Color(0.58, 0.60, 0.66))
	# バイザースリット（2本）
	_rect(img, 23, 18, 18,  3, Color(0.14, 0.14, 0.20))
	_rect(img, 23, 23, 18,  3, Color(0.14, 0.14, 0.20))
	# 胴鎧（重装）
	_rect(img, 13, 30, 38, 30, Color(0.58, 0.60, 0.66))
	_rect(img, 21, 34, 22, 20, Color(0.50, 0.52, 0.58))
	_rect(img, 29, 34,  6, 20, Color(0.62, 0.65, 0.70))
	# ベルト
	_rect(img, 13, 58, 38,  5, Color(0.38, 0.30, 0.14))
	# 脚甲
	_rect(img, 15, 63, 14, 17, Color(0.55, 0.58, 0.64))
	_rect(img, 35, 63, 14, 17, Color(0.55, 0.58, 0.64))
	# ブーツ
	_ellipse(img, 22, 80, 11,  4, Color(0.28, 0.22, 0.14))
	_ellipse(img, 42, 80, 11,  4, Color(0.28, 0.22, 0.14))
	# 肩当て
	_ellipse(img, 11, 32, 10,  8, Color(0.60, 0.62, 0.68))
	_ellipse(img, 53, 32, 10,  8, Color(0.60, 0.62, 0.68))
	# 左腕＋大盾（青）
	_rect(img,  1, 28, 12, 30, Color(0.55, 0.58, 0.64))
	_rect(img, -1, 26, 14, 36, Color(0.34, 0.34, 0.72))
	_rect(img,  2, 30,  8,  5, Color(0.85, 0.72, 0.22))
	_rect(img,  6, 26,  2, 36, Color(0.26, 0.26, 0.62))
	# 右腕＋大剣
	_rect(img, 51, 28, 12, 30, Color(0.55, 0.58, 0.64))
	_rect(img, 58,  2,  5, 42, Color(0.80, 0.84, 0.92))
	_rect(img, 50, 36, 14,  5, Color(0.68, 0.58, 0.22))
	_circle(img, 60,  3,  4, Color(0.82, 0.85, 0.94))
	return ImageTexture.create_from_image(img)

# 聖騎士 - 黄金鎧・後光・聖剣
static func _make_holy_knight() -> ImageTexture:
	var img = _blank(SW, SH)
	# 後光（黄金リング）
	_circle_ring(img, 32,  7, 11, 3, Color(1.00, 0.95, 0.35))
	# 兜（黄金）
	_ellipse(img, 32, 14, 14, 12, Color(0.88, 0.74, 0.14))
	_rect(img, 19, 14, 26, 16, Color(0.88, 0.74, 0.14))
	# 兜の十字
	_rect(img, 30, 13,  4, 16, Color(1.00, 1.00, 0.90))
	_rect(img, 23, 18, 18,  4, Color(1.00, 1.00, 0.90))
	# バイザー
	_rect(img, 23, 23, 18,  4, Color(0.30, 0.24, 0.04))
	# 白マント（両サイド）
	_rect(img,  8, 30,  8, 44, Color(0.96, 0.96, 0.96))
	_rect(img, 48, 30,  8, 44, Color(0.96, 0.96, 0.96))
	# 黄金鎧（胴）
	_rect(img, 14, 30, 36, 30, Color(0.88, 0.74, 0.14))
	# 胸の十字紋章
	_rect(img, 30, 33,  4, 22, Color(1.00, 1.00, 0.90))
	_rect(img, 22, 41, 20,  4, Color(1.00, 1.00, 0.90))
	# ベルト
	_rect(img, 14, 58, 36,  5, Color(0.65, 0.52, 0.10))
	# 脚甲（黄金）
	_rect(img, 16, 63, 14, 17, Color(0.85, 0.72, 0.14))
	_rect(img, 34, 63, 14, 17, Color(0.85, 0.72, 0.14))
	# ブーツ
	_ellipse(img, 23, 80, 11,  4, Color(0.55, 0.44, 0.08))
	_ellipse(img, 41, 80, 11,  4, Color(0.55, 0.44, 0.08))
	# 肩当て
	_ellipse(img, 11, 32, 10,  8, Color(0.90, 0.76, 0.18))
	_ellipse(img, 53, 32, 10,  8, Color(0.90, 0.76, 0.18))
	# 左腕＋黄金盾
	_rect(img,  0, 28, 10, 30, Color(0.85, 0.72, 0.14))
	_rect(img, -2, 26, 12, 36, Color(0.88, 0.74, 0.14))
	_circle(img,  4, 44,  6, Color(1.00, 0.95, 0.35))
	# 右腕＋聖剣（白く輝く）
	_rect(img, 54, 28, 10, 30, Color(0.85, 0.72, 0.14))
	_rect(img, 58,  2,  4, 44, Color(0.96, 0.98, 1.00))
	_rect(img, 52, 34, 12,  5, Color(0.88, 0.74, 0.14))
	_circle(img, 60,  2,  5, Color(1.00, 1.00, 0.85))
	return ImageTexture.create_from_image(img)

# ===================== CARD ICONS =====================

# 攻撃アイコン（爪痕3本）
static func _make_icon_attack() -> ImageTexture:
	var img = _blank(IW, IH)
	_circle(img, 20, 20, 17, Color(0.50, 0.05, 0.05, 0.35))
	_thick_line(img, 10,  5, 22, 36, 3, Color(0.95, 0.25, 0.10))
	_thick_line(img, 18,  4, 30, 35, 3, Color(0.95, 0.25, 0.10))
	_thick_line(img, 26,  5, 38, 36, 3, Color(0.95, 0.25, 0.10))
	return ImageTexture.create_from_image(img)

# 防御アイコン（盾）
static func _make_icon_defense() -> ImageTexture:
	var img = _blank(IW, IH)
	# 盾上部（半円）
	_ellipse(img, 20, 17, 16, 14, Color(0.18, 0.30, 0.72))
	# 盾下部（三角）
	for y in range(17, 37):
		var w2: int = max(1, int((36.0 - y) * 16.0 / 19.0))
		_rect(img, 20 - w2, y, w2 * 2, 1, Color(0.18, 0.30, 0.72))
	# 盾の紋様（十字）
	_rect(img, 18,  6,  4, 28, Color(0.60, 0.75, 1.00))
	_rect(img,  7, 16, 26,  4, Color(0.60, 0.75, 1.00))
	return ImageTexture.create_from_image(img)

# スキルアイコン（魔法陣）
static func _make_icon_skill() -> ImageTexture:
	var img = _blank(IW, IH)
	_circle_ring(img, 20, 20, 17, 3, Color(0.60, 0.20, 0.90))
	_circle_ring(img, 20, 20, 10, 2, Color(0.82, 0.44, 1.00))
	for i in range(6):
		var a: float = i * PI / 3.0
		var x1: int  = int(20.0 + cos(a) * 13.0)
		var y1: int  = int(20.0 + sin(a) * 13.0)
		var x2: int  = int(20.0 + cos(a + PI) * 13.0)
		var y2: int  = int(20.0 + sin(a + PI) * 13.0)
		_thick_line(img, x1, y1, x2, y2, 1, Color(0.90, 0.60, 1.00))
	_circle(img, 20, 20,  3, Color(1.00, 0.90, 1.00))
	return ImageTexture.create_from_image(img)

# パワーアイコン（炎の拳）
static func _make_icon_power() -> ImageTexture:
	var img = _blank(IW, IH)
	# 炎の背景
	_circle(img, 20, 24, 14, Color(0.90, 0.45, 0.05, 0.45))
	_circle(img, 20, 21, 10, Color(1.00, 0.70, 0.10, 0.65))
	# 拳
	_ellipse(img, 20, 25, 10,  9, Color(0.65, 0.42, 0.22))
	# 指（4本）
	for i in range(4):
		_rect(img, 10 + i * 5, 13, 4, 12, Color(0.72, 0.48, 0.28))
	# 親指
	_rect(img,  5, 18,  7,  6, Color(0.72, 0.48, 0.28))
	# 炎の先端
	_circle(img, 16,  5,  5, Color(1.00, 0.85, 0.20))
	_circle(img, 24,  3,  4, Color(1.00, 0.70, 0.10))
	_circle(img, 20,  1,  3, Color(1.00, 0.95, 0.60))
	return ImageTexture.create_from_image(img)

# ===================== DRAW HELPERS =====================

static func _blank(w: int, h: int) -> Image:
	var img: Image = Image.create(w, h, false, Image.FORMAT_RGBA8)
	img.fill(Color(0, 0, 0, 0))
	return img

static func _circle(img: Image, cx: int, cy: int, r: int, col: Color) -> void:
	var w: int = img.get_width()
	var h: int = img.get_height()
	for y in range(cy - r, cy + r + 1):
		for x in range(cx - r, cx + r + 1):
			if x >= 0 and x < w and y >= 0 and y < h:
				if (x - cx) * (x - cx) + (y - cy) * (y - cy) <= r * r:
					_blend(img, x, y, col)

static func _circle_ring(img: Image, cx: int, cy: int, r: int, thick: int, col: Color) -> void:
	var w: int   = img.get_width()
	var h: int   = img.get_height()
	var inn: int = (r - thick) * (r - thick)
	var out: int = r * r
	for y in range(cy - r - 1, cy + r + 2):
		for x in range(cx - r - 1, cx + r + 2):
			if x >= 0 and x < w and y >= 0 and y < h:
				var d2: int = (x - cx) * (x - cx) + (y - cy) * (y - cy)
				if d2 >= inn and d2 <= out:
					_blend(img, x, y, col)

static func _ellipse(img: Image, cx: int, cy: int, rx: int, ry: int, col: Color) -> void:
	var w: int = img.get_width()
	var h: int = img.get_height()
	for y in range(cy - ry, cy + ry + 1):
		for x in range(cx - rx, cx + rx + 1):
			if x >= 0 and x < w and y >= 0 and y < h:
				var nx: float = float(x - cx) / rx
				var ny: float = float(y - cy) / ry
				if nx * nx + ny * ny <= 1.0:
					_blend(img, x, y, col)

static func _rect(img: Image, x: int, y: int, rw: int, rh: int, col: Color) -> void:
	var w: int = img.get_width()
	var h: int = img.get_height()
	for py in range(y, y + rh):
		for px in range(x, x + rw):
			if px >= 0 and px < w and py >= 0 and py < h:
				_blend(img, px, py, col)

static func _thick_line(img: Image, x1: int, y1: int, x2: int, y2: int, thick: int, col: Color) -> void:
	var steps: int = max(abs(x2 - x1), abs(y2 - y1))
	if steps == 0:
		_circle(img, x1, y1, thick, col)
		return
	for i in range(steps + 1):
		var t: float = float(i) / steps
		var px: int  = int(x1 + (x2 - x1) * t)
		var py: int  = int(y1 + (y2 - y1) * t)
		_circle(img, px, py, thick, col)

static func _blend(img: Image, x: int, y: int, col: Color) -> void:
	if col.a >= 0.99:
		img.set_pixel(x, y, col)
	else:
		var base: Color = img.get_pixel(x, y)
		var a: float    = col.a
		img.set_pixel(x, y, Color(
			base.r * (1.0 - a) + col.r * a,
			base.g * (1.0 - a) + col.g * a,
			base.b * (1.0 - a) + col.b * a,
			max(base.a, col.a)
		))
