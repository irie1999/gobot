extends Node2D
# =============================================================
#  闇喰らいの怪物  -  メインシーン
#  画面サイズ: 800 x 600
#  ProjectSettings > Display > Window > Viewport Width/Height
# =============================================================

const SCREEN_W  = 800
const SCREEN_H  = 600
const CARD_W    = 105
const CARD_H    = 145
const CARD_GAP  = 8
const ENEMY_W   = 155
const ENEMY_H   = 130
const ENEMY_GAP = 16

enum State { TITLE, PLAYER_TURN, ENEMY_TURN, REWARD, GAME_OVER }

var state: State = State.TITLE

# --- プレイヤーステータス ---
var player_hp:         int = 80
var player_max_hp:     int = 80
var player_block:      int = 0
var player_energy:     int = 3
var player_max_energy: int = 3
var player_strength:   int = 0

# --- デッキ ---
var deck:    Array = []
var hand:    Array = []
var discard: Array = []

# --- 敵リスト (Dictionary の配列) ---
var enemies: Array = []
var selected_enemy: int = 0

# --- ゲーム進行 ---
var wave:     int    = 1
var score:    int    = 0
var log_text: String = ""

# =============================================================
#  起動
# =============================================================
func _ready() -> void:
	randomize()
	show_title()

# =============================================================
#  タイトル画面
# =============================================================
func show_title() -> void:
	state = State.TITLE
	_clear()

	_panel(Vector2.ZERO, Vector2(SCREEN_W, SCREEN_H), Color(0.04, 0.01, 0.07))

	_label("闇喰らいの怪物", Vector2(150, 160), 48, Color(0.85, 0.15, 0.15))
	_label("～暗黒の生存記録～",  Vector2(255, 230), 20, Color(0.6, 0.4, 0.85))
	_label(
		"汝は闇より生まれし怪物。\n押し寄せる人間どもをカードで退けよ。\n\n【操作】カードをクリックで使用\n        敵をクリックでターゲット変更\n        ターン終了ボタンで次の敵ターンへ",
		Vector2(210, 280), 15, Color(0.8, 0.8, 0.8)
	)

	_button("ゲーム開始", Vector2(300, 460), Vector2(200, 55),
		func(): start_game())

func start_game() -> void:
	player_hp         = 80
	player_max_hp     = 80
	player_block      = 0
	player_strength   = 0
	player_energy     = player_max_energy
	wave  = 1
	score = 0

	deck    = GameData.STARTING_DECK.duplicate()
	deck.shuffle()
	hand    = []
	discard = []

	_start_wave()

# =============================================================
#  ウェーブ開始
# =============================================================
func _start_wave() -> void:
	enemies = []
	selected_enemy = 0

	for etype in GameData.get_wave_enemies(wave):
		var d: Dictionary = GameData.ENEMIES[etype].duplicate()
		enemies.append({
			"type":   etype,
			"name":   d["name"],
			"hp":     d["max_hp"],
			"max_hp": d["max_hp"],
			"attack": d["attack"],
			"block":  0,
			"color":  d["color"],
			"poison": 0,
			"weak":   0,
		})

	log_text = "ウェーブ %d 開始！" % wave
	_start_player_turn()

func _start_player_turn() -> void:
	state         = State.PLAYER_TURN
	player_energy = player_max_energy
	player_block  = 0
	_draw_cards(5)
	if log_text.is_empty() or log_text.begins_with("ウェーブ"):
		log_text = "カードを選んで攻撃しよう！"
	redraw()

# =============================================================
#  デッキ操作
# =============================================================
func _draw_cards(n: int) -> void:
	for _i in range(n):
		if deck.is_empty():
			if discard.is_empty():
				break
			deck    = discard.duplicate()
			discard = []
			deck.shuffle()
		hand.append(deck.pop_back())

# =============================================================
#  メイン描画
# =============================================================
func _clear() -> void:
	for c in get_children():
		c.queue_free()

func redraw() -> void:
	_clear()
	_draw_background()
	_draw_top_bar()
	_draw_enemy_area()
	_draw_log()
	_draw_player_stats()
	_draw_hand()
	_draw_end_turn_btn()

# ---- 背景 ----
func _draw_background() -> void:
	_panel(Vector2.ZERO, Vector2(SCREEN_W, SCREEN_H), Color(0.04, 0.01, 0.07))

# ---- トップバー ----
func _draw_top_bar() -> void:
	_panel(Vector2.ZERO, Vector2(SCREEN_W, 44), Color(0.10, 0.04, 0.16))
	_label("ウェーブ %d" % wave,             Vector2(16, 10), 16, Color(0.95, 0.55, 0.20))
	_label("スコア: %d" % score,             Vector2(300, 10), 15, Color(0.85, 0.85, 0.85))
	_label("山札:%d 捨:%d" % [deck.size(), discard.size()],
	                                          Vector2(580, 10), 14, Color(0.55, 0.85, 0.55))

# ---- 敵エリア ----
func _draw_enemy_area() -> void:
	_panel(Vector2(0, 48), Vector2(SCREEN_W, 185), Color(0.07, 0.03, 0.11))

	if enemies.is_empty():
		return

	var total_w: float = enemies.size() * ENEMY_W + (enemies.size() - 1) * ENEMY_GAP
	var sx: float = (SCREEN_W - total_w) / 2.0

	for i in range(enemies.size()):
		var e: Dictionary = enemies[i]
		var ex: float = sx + i * (ENEMY_W + ENEMY_GAP)
		var ey: float = 56.0

		# ボックス (選択中は明るく)
		var col: Color = e["color"]
		var box_col: Color = col * (0.9 if i == selected_enemy else 0.5)
		box_col.a = 1.0
		var box: ColorRect = _panel(Vector2(ex, ey), Vector2(ENEMY_W, ENEMY_H), box_col)
		box.mouse_filter = Control.MOUSE_FILTER_STOP
		var idx: int = i
		box.gui_input.connect(func(ev: InputEvent) -> void: _on_enemy_click(ev, idx))

		# 名前
		_label(e["name"], Vector2(ex + 8, ey + 4), 14, Color(1, 1, 1))

		# HPバー
		_panel(Vector2(ex + 8, ey + 26), Vector2(ENEMY_W - 16, 10), Color(0.30, 0.0, 0.0))
		var ratio: float = float(e["hp"]) / float(e["max_hp"])
		_panel(Vector2(ex + 8, ey + 26), Vector2((ENEMY_W - 16) * ratio, 10), Color(0.85, 0.1, 0.1))
		_label("%d/%d" % [e["hp"], e["max_hp"]], Vector2(ex + 8, ey + 38), 11, Color(0.9, 0.9, 0.9))

		# 攻撃予告
		_label("[攻]%d" % e["attack"], Vector2(ex + 8, ey + 58), 13, Color(1.0, 0.5, 0.3))

		# ブロック
		if e["block"] > 0:
			_label("[防]%d" % e["block"], Vector2(ex + 75, ey + 58), 13, Color(0.5, 0.7, 1.0))

		# 状態異常
		var sx2: float = ex + 8
		if e["poison"] > 0:
			_label("毒%d" % e["poison"], Vector2(sx2, ey + 82), 11, Color(0.4, 1.0, 0.3))
			sx2 += 45
		if e["weak"] > 0:
			_label("弱%d" % e["weak"], Vector2(sx2, ey + 82), 11, Color(1.0, 0.85, 0.2))

# ---- バトルログ ----
func _draw_log() -> void:
	_panel(Vector2(0, 236), Vector2(SCREEN_W, 32), Color(0.06, 0.02, 0.10))
	_label(log_text, Vector2(16, 241), 13, Color(0.95, 0.88, 0.70))

# ---- プレイヤーステータス ----
func _draw_player_stats() -> void:
	_panel(Vector2(0, 271), Vector2(SCREEN_W, 68), Color(0.07, 0.02, 0.11))

	# HP バー
	_panel(Vector2(16, 280), Vector2(210, 18), Color(0.28, 0.0, 0.0))
	var hp_r: float = float(player_hp) / float(player_max_hp)
	_panel(Vector2(16, 280), Vector2(210 * hp_r, 18), Color(0.85, 0.1, 0.1))
	_label("HP %d/%d" % [player_hp, player_max_hp], Vector2(20, 280), 13, Color(1, 1, 1))

	_label("[防] ブロック: %d" % player_block, Vector2(16, 302), 13, Color(0.5, 0.7, 1.0))
	_label("[E] %d / %d" % [player_energy, player_max_energy], Vector2(250, 280), 14, Color(0.3, 0.85, 1.0))
	if player_strength > 0:
		_label("筋力: +%d" % player_strength, Vector2(250, 302), 13, Color(1.0, 0.65, 0.2))

# ---- 手札 ----
func _draw_hand() -> void:
	_panel(Vector2(0, 342), Vector2(SCREEN_W, 205), Color(0.04, 0.01, 0.07))

	var show_n: int  = min(hand.size(), 7)
	if show_n == 0:
		return

	var total_w: float = show_n * CARD_W + (show_n - 1) * CARD_GAP
	var sx: float      = (SCREEN_W - total_w) / 2.0

	for i in range(show_n):
		var cid: String    = hand[i]
		var cd: Dictionary = GameData.CARDS[cid]
		var cx: float = sx + i * (CARD_W + CARD_GAP)
		var cy: float = 348.0

		var can_play: bool = player_energy >= cd["cost"]

		# カード背景色
		var bg_col: Color
		if not can_play:
			bg_col = Color(0.14, 0.14, 0.14)
		else:
			match cd["type"]:
				"attack":  bg_col = Color(0.38, 0.08, 0.08)
				"defense": bg_col = Color(0.08, 0.18, 0.38)
				"skill":   bg_col = Color(0.18, 0.08, 0.32)
				"power":   bg_col = Color(0.32, 0.18, 0.04)
				_:         bg_col = Color(0.2, 0.2, 0.2)

		var box: ColorRect = _panel(Vector2(cx, cy), Vector2(CARD_W, CARD_H), bg_col)
		box.mouse_filter   = Control.MOUSE_FILTER_STOP
		var idx: int = i
		box.gui_input.connect(func(ev: InputEvent) -> void: _on_card_click(ev, idx))

		# コスト
		_panel(Vector2(cx + 4, cy + 4), Vector2(22, 20),
			Color(0.1, 0.3, 0.5) if can_play else Color(0.25, 0.25, 0.25))
		_label(str(cd["cost"]), Vector2(cx + 9, cy + 4), 13,
			Color(1, 1, 1) if can_play else Color(0.5, 0.5, 0.5))

		# 名前
		_label(cd["name"], Vector2(cx + 5, cy + 30), 12,
			Color(1, 1, 0.8) if can_play else Color(0.45, 0.45, 0.45))

		# 仕切り
		_panel(Vector2(cx, cy + 52), Vector2(CARD_W, 2), Color(0.25, 0.25, 0.25))

		# タイプアイコン
		var type_str: String
		match cd["type"]:
			"attack":  type_str = "[攻]"
			"defense": type_str = "[防]"
			"skill":   type_str = "[技]"
			"power":   type_str = "[力]"
			_:         type_str = "[ ]"
		_label(type_str, Vector2(cx + 32, cy + 58), 20,
			Color(1, 1, 1) if can_play else Color(0.4, 0.4, 0.4))

		# 説明
		var desc_lbl: Label = Label.new()
		desc_lbl.text      = cd["desc"]
		desc_lbl.position  = Vector2(cx + 5, cy + 96)
		desc_lbl.size      = Vector2(CARD_W - 10, 44)
		desc_lbl.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
		desc_lbl.add_theme_font_size_override("font_size", 10)
		desc_lbl.modulate  = Color(0.88, 0.88, 0.88) if can_play else Color(0.38, 0.38, 0.38)
		add_child(desc_lbl)

# ---- ターン終了ボタン ----
func _draw_end_turn_btn() -> void:
	_button("ターン終了", Vector2(640, 548), Vector2(140, 42),
		func(): _end_player_turn())

# =============================================================
#  入力ハンドラ
# =============================================================
func _on_card_click(ev: InputEvent, idx: int) -> void:
	if not ev is InputEventMouseButton:
		return
	if not (ev as InputEventMouseButton).pressed:
		return
	if (ev as InputEventMouseButton).button_index != MOUSE_BUTTON_LEFT:
		return
	if state != State.PLAYER_TURN:
		return
	_play_card(idx)

func _on_enemy_click(ev: InputEvent, idx: int) -> void:
	if not ev is InputEventMouseButton:
		return
	if not (ev as InputEventMouseButton).pressed:
		return
	if (ev as InputEventMouseButton).button_index != MOUSE_BUTTON_LEFT:
		return
	selected_enemy = idx
	log_text = "ターゲット: " + enemies[idx]["name"]
	redraw()

# =============================================================
#  カード使用
# =============================================================
func _play_card(idx: int) -> void:
	if idx >= hand.size():
		return
	var cid: String    = hand[idx]
	var cd: Dictionary = GameData.CARDS[cid]

	if player_energy < cd["cost"]:
		log_text = "エネルギーが足りない！"
		redraw()
		return
	if cd["type"] == "attack" and enemies.is_empty():
		log_text = "攻撃対象がいない！"
		redraw()
		return

	player_energy -= cd["cost"]
	hand.remove_at(idx)
	discard.append(cid)

	var msgs: Array = []

	# ダメージ
	if cd["damage"] > 0:
		var base: int = cd["damage"] + player_strength
		if cd["aoe"]:
			for e in enemies:
				msgs.append("%sに%dダメージ" % [e["name"], _hit(e, base)])
		else:
			var e: Dictionary = enemies[selected_enemy]
			msgs.append("%sに%dダメージ" % [e["name"], _hit(e, base)])

	# ブロック
	if cd["block"] > 0:
		player_block += cd["block"]
		msgs.append("ブロック%d" % cd["block"])

	# 回復
	if cd["heal"] > 0:
		player_hp = min(player_hp + cd["heal"], player_max_hp)
		msgs.append("%d回復" % cd["heal"])

	# ドロー
	if cd["draw"] > 0:
		_draw_cards(cd["draw"])
		msgs.append("%d枚ドロー" % cd["draw"])

	# 毒
	if cd["poison"] > 0:
		if cd["aoe"]:
			for e in enemies:
				e["poison"] += cd["poison"]
		else:
			enemies[selected_enemy]["poison"] += cd["poison"]
		msgs.append("毒%d付与" % cd["poison"])

	# 弱体
	if cd["weak"] > 0:
		if cd["aoe"]:
			for e in enemies:
				e["weak"] += cd["weak"]
		else:
			enemies[selected_enemy]["weak"] += cd["weak"]
		msgs.append("弱体%d付与" % cd["weak"])

	# 筋力
	if cd["strength"] > 0:
		player_strength += cd["strength"]
		msgs.append("筋力+%d" % cd["strength"])

	log_text = cd["name"] + "！ " + "、".join(msgs)

	_remove_dead()

	if selected_enemy >= enemies.size() and not enemies.is_empty():
		selected_enemy = enemies.size() - 1

	if enemies.is_empty():
		_wave_clear()
		return

	redraw()

func _hit(e: Dictionary, dmg: int) -> int:
	var actual: int = int(dmg * 0.75) if e["weak"] > 0 else dmg
	var blocked: int = min(e["block"], actual)
	e["block"] -= blocked
	actual      -= blocked
	e["hp"]     -= actual
	return actual

func _remove_dead() -> void:
	enemies = enemies.filter(func(e: Dictionary) -> bool: return e["hp"] > 0)

# =============================================================
#  ターン終了 / 敵のターン
# =============================================================
func _end_player_turn() -> void:
	if state != State.PLAYER_TURN:
		return
	state = State.ENEMY_TURN

	discard.append_array(hand)
	hand = []

	var msgs: Array = []

	for e in enemies:
		# 毒ダメージ
		if e["poison"] > 0:
			e["hp"]     -= e["poison"]
			msgs.append("%sが毒で%dダメージ" % [e["name"], e["poison"]])
			e["poison"]  = max(0, e["poison"] - 1)
		# 弱体カウントダウン
		if e["weak"] > 0:
			e["weak"] = max(0, e["weak"] - 1)
		# 攻撃
		if e["hp"] > 0:
			var atk: int     = e["attack"]
			var absorbed: int = min(player_block, atk)
			player_block     -= absorbed
			var actual: int   = atk - absorbed
			player_hp        -= actual
			msgs.append("%sの攻撃: %dダメージ" % [e["name"], actual])

	_remove_dead()
	score   += 10
	log_text = "  ".join(msgs) if not msgs.is_empty() else "敵のターン終了"

	if player_hp <= 0:
		player_hp = 0
		_game_over()
		return

	if enemies.is_empty():
		_wave_clear()
		return

	_start_player_turn()

# =============================================================
#  ウェーブクリア & 報酬
# =============================================================
func _wave_clear() -> void:
	state   = State.REWARD
	score  += wave * 50
	log_text = "ウェーブ %d クリア！" % wave
	_show_reward()

func _show_reward() -> void:
	_clear()
	_panel(Vector2.ZERO, Vector2(SCREEN_W, SCREEN_H), Color(0.04, 0.01, 0.07))

	_label("ウェーブ %d クリア！" % wave, Vector2(240, 30), 30, Color(0.95, 0.75, 0.2))
	_label("スコア: %d" % score,          Vector2(330, 80), 16, Color(0.85, 0.85, 0.85))
	_label("カードを1枚選んでデッキに追加：", Vector2(190, 120), 15, Color(0.65, 0.95, 0.65))

	var rewards: Array = GameData.get_random_rewards()
	for i in range(rewards.size()):
		var cid: String    = rewards[i]
		var cd: Dictionary = GameData.CARDS[cid]
		var rx: float      = 70.0 + i * 220.0
		var ry: float      = 155.0

		var col: Color
		match cd["type"]:
			"attack":  col = Color(0.38, 0.08, 0.08)
			"defense": col = Color(0.08, 0.18, 0.38)
			"skill":   col = Color(0.18, 0.08, 0.32)
			"power":   col = Color(0.32, 0.18, 0.04)
			_:         col = Color(0.2, 0.2, 0.2)

		_panel(Vector2(rx, ry), Vector2(190, 220), col)
		_label("コスト: %d" % cd["cost"],  Vector2(rx + 10, ry + 10), 13, Color(0.5, 0.85, 1.0))
		_label(cd["name"],                 Vector2(rx + 10, ry + 36), 20, Color(1, 1, 0.8))
		var tp: String
		match cd["type"]:
			"attack":  tp = "[攻] 攻撃"
			"defense": tp = "[防] 防御"
			"skill":   tp = "[技] スキル"
			"power":   tp = "[力] パワー"
			_:         tp = ""
		_label(tp, Vector2(rx + 10, ry + 68), 13, Color(0.8, 0.8, 0.8))

		var dl: Label = Label.new()
		dl.text       = cd["desc"]
		dl.position   = Vector2(rx + 10, ry + 96)
		dl.size       = Vector2(170, 60)
		dl.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
		dl.add_theme_font_size_override("font_size", 13)
		dl.modulate   = Color(0.9, 0.9, 0.9)
		add_child(dl)

		var pick: String = cid
		_button("選ぶ", Vector2(rx + 25, ry + 168), Vector2(140, 38),
			func(): _pick_card(pick))

	_button("スキップ",             Vector2(310, 400), Vector2(180, 40), func(): _next_wave())
	_button("スキップしてHP+20回復", Vector2(240, 456), Vector2(320, 40), func(): _heal_skip())

func _pick_card(cid: String) -> void:
	discard.append(cid)
	_next_wave()

func _heal_skip() -> void:
	player_hp = min(player_hp + 20, player_max_hp)
	_next_wave()

func _next_wave() -> void:
	wave += 1
	_start_wave()

# =============================================================
#  ゲームオーバー
# =============================================================
func _game_over() -> void:
	state = State.GAME_OVER
	_clear()

	_panel(Vector2.ZERO, Vector2(SCREEN_W, SCREEN_H), Color(0.02, 0.00, 0.04))
	_label("GAME  OVER",        Vector2(170, 140), 58, Color(0.75, 0.08, 0.08))
	_label("到達ウェーブ: %d" % wave, Vector2(275, 270), 26, Color(0.85, 0.60, 0.25))
	_label("最終スコア: %d" % score,  Vector2(285, 320), 24, Color(0.85, 0.85, 0.85))

	_button("もう一度挑戦", Vector2(300, 420), Vector2(200, 55), func(): start_game())
	_button("タイトルへ",   Vector2(300, 496), Vector2(200, 45), func(): show_title())

# =============================================================
#  UIヘルパー
# =============================================================
func _panel(pos: Vector2, size: Vector2, col: Color) -> ColorRect:
	var r: ColorRect = ColorRect.new()
	r.position = pos
	r.size     = size
	r.color    = col
	add_child(r)
	return r

func _label(text: String, pos: Vector2, font_size: int, col: Color) -> Label:
	var l: Label = Label.new()
	l.text     = text
	l.position = pos
	l.add_theme_font_size_override("font_size", font_size)
	l.modulate = col
	add_child(l)
	return l

func _button(text: String, pos: Vector2, size: Vector2, callback: Callable) -> Button:
	var b: Button = Button.new()
	b.text     = text
	b.position = pos
	b.size     = size
	b.pressed.connect(callback)
	add_child(b)
	return b
