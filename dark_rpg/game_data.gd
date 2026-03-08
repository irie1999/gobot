class_name GameData

# -------------------------------------------------------
# カードデータ
# type: "attack" / "defense" / "skill" / "power"
# aoe: true のとき全敵対象
# -------------------------------------------------------
const CARDS: Dictionary = {
	"claw_strike": {
		"name": "爪撃", "cost": 1, "type": "attack",
		"damage": 6, "block": 0, "heal": 0, "draw": 0,
		"aoe": false, "poison": 0, "weak": 0, "strength": 0,
		"desc": "敵に6ダメージ"
	},
	"shadow_shield": {
		"name": "影の盾", "cost": 1, "type": "defense",
		"damage": 0, "block": 5, "heal": 0, "draw": 0,
		"aoe": false, "poison": 0, "weak": 0, "strength": 0,
		"desc": "5ブロック獲得"
	},
	"devour": {
		"name": "貪食", "cost": 2, "type": "attack",
		"damage": 10, "block": 0, "heal": 4, "draw": 0,
		"aoe": false, "poison": 0, "weak": 0, "strength": 0,
		"desc": "10ダメージ+4HP回復"
	},
	"dark_breath": {
		"name": "暗黒の息吹", "cost": 2, "type": "attack",
		"damage": 4, "block": 0, "heal": 0, "draw": 0,
		"aoe": true, "poison": 0, "weak": 0, "strength": 0,
		"desc": "全敵に4ダメージ"
	},
	"bite": {
		"name": "噛みつき", "cost": 2, "type": "attack",
		"damage": 12, "block": 0, "heal": 0, "draw": 0,
		"aoe": false, "poison": 0, "weak": 0, "strength": 0,
		"desc": "敵に12ダメージ"
	},
	"curse": {
		"name": "呪い", "cost": 1, "type": "skill",
		"damage": 0, "block": 0, "heal": 0, "draw": 0,
		"aoe": false, "poison": 3, "weak": 0, "strength": 0,
		"desc": "敵に毒3付与"
	},
	"nightmare": {
		"name": "悪夢", "cost": 2, "type": "skill",
		"damage": 0, "block": 0, "heal": 0, "draw": 0,
		"aoe": true, "poison": 0, "weak": 2, "strength": 0,
		"desc": "全敵に弱体2付与"
	},
	"bone_armor": {
		"name": "骨の鎧", "cost": 2, "type": "defense",
		"damage": 0, "block": 10, "heal": 0, "draw": 0,
		"aoe": false, "poison": 0, "weak": 0, "strength": 0,
		"desc": "10ブロック獲得"
	},
	"soul_wail": {
		"name": "魂の嘆き", "cost": 0, "type": "skill",
		"damage": 0, "block": 0, "heal": 0, "draw": 2,
		"aoe": false, "poison": 0, "weak": 0, "strength": 0,
		"desc": "カードを2枚ドロー"
	},
	"monster_strength": {
		"name": "怪力", "cost": 1, "type": "power",
		"damage": 0, "block": 0, "heal": 0, "draw": 0,
		"aoe": false, "poison": 0, "weak": 0, "strength": 2,
		"desc": "筋力を+2（永続）"
	},
}

# 初期デッキ
const STARTING_DECK: Array = [
	"claw_strike", "claw_strike", "claw_strike", "claw_strike",
	"shadow_shield", "shadow_shield", "shadow_shield", "shadow_shield",
	"devour", "dark_breath",
]

# 報酬候補
const REWARD_POOL: Array = [
	"bite", "curse", "nightmare", "bone_armor",
	"soul_wail", "monster_strength", "devour", "dark_breath",
]

# -------------------------------------------------------
# 敵データ
# -------------------------------------------------------
const ENEMIES: Dictionary = {
	"villager": {
		"name": "村人", "max_hp": 20, "attack": 4,
		"color": Color(0.75, 0.65, 0.55),
	},
	"soldier": {
		"name": "兵士", "max_hp": 35, "attack": 7,
		"color": Color(0.45, 0.45, 0.65),
	},
	"knight": {
		"name": "騎士", "max_hp": 55, "attack": 10,
		"color": Color(0.55, 0.55, 0.55),
	},
	"holy_knight": {
		"name": "聖騎士", "max_hp": 75, "attack": 13,
		"color": Color(0.85, 0.85, 0.45),
	},
}

# ウェーブ定義
const WAVES: Array = [
	["villager"],
	["villager", "villager"],
	["soldier"],
	["soldier", "villager"],
	["soldier", "soldier"],
	["knight"],
	["knight", "soldier"],
	["holy_knight"],
	["holy_knight", "knight"],
	["holy_knight", "holy_knight"],
]

static func get_wave_enemies(wave: int) -> Array:
	if wave - 1 < WAVES.size():
		return WAVES[wave - 1]
	var extras: Array = [
		["holy_knight", "knight"],
		["holy_knight", "soldier", "soldier"],
		["holy_knight", "holy_knight"],
	]
	return extras[randi() % extras.size()]

static func get_random_rewards() -> Array:
	var pool: Array = REWARD_POOL.duplicate()
	pool.shuffle()
	return pool.slice(0, 3)
