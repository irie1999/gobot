extends Node2D

const GRID_SIZE = 3
const TILE_SIZE = 120
const MARGIN = 8
var grid = []
var empty_pos = Vector2i(2, 2)
var move_count = 0

func _ready():
	initialize_grid()
	shuffle_grid()
	redraw()

func initialize_grid():
	grid = []
	var num = 1
	for row in range(GRID_SIZE):
		var row_data = []
		for col in range(GRID_SIZE):
			if row == GRID_SIZE - 1 and col == GRID_SIZE - 1:
				row_data.append(0)
			else:
				row_data.append(num)
				num += 1
		grid.append(row_data)
	empty_pos = Vector2i(GRID_SIZE - 1, GRID_SIZE - 1)

func shuffle_grid():
	var dirs = [Vector2i(-1,0), Vector2i(1,0), Vector2i(0,-1), Vector2i(0,1)]
	for i in range(300):
		var candidates = []
		for d in dirs:
			var p = empty_pos + d
			if p.x >= 0 and p.x < GRID_SIZE and p.y >= 0 and p.y < GRID_SIZE:
				candidates.append(p)
		move_tile(candidates[randi() % candidates.size()])
	move_count = 0

func move_tile(tile_pos: Vector2i):
	grid[empty_pos.y][empty_pos.x] = grid[tile_pos.y][tile_pos.x]
	grid[tile_pos.y][tile_pos.x] = 0
	empty_pos = tile_pos

func redraw():
	for child in get_children():
		child.queue_free()

	draw_goal_panel()

	for row in range(GRID_SIZE):
		for col in range(GRID_SIZE):
			var value = grid[row][col]
			if value == 0:
				continue
			var btn = Button.new()
			btn.text = str(value)
			btn.size = Vector2(TILE_SIZE, TILE_SIZE)
			btn.position = Vector2(
				col * (TILE_SIZE + MARGIN) + 20,
				row * (TILE_SIZE + MARGIN) + 60
			)
			var c = col
			var r = row
			btn.pressed.connect(func(): on_tile_pressed(Vector2i(c, r)))
			add_child(btn)

	var lbl = Label.new()
	lbl.text = "手数: %d" % move_count
	lbl.position = Vector2(20, 20)
	add_child(lbl)

	var goal_lbl = Label.new()
	goal_lbl.text = "【目標】1〜8を順番に並べ、\n右下を空白にしよう！"
	goal_lbl.position = Vector2(440, 20)
	add_child(goal_lbl)

func draw_goal_panel():
	const SMALL = 52
	const SM = 4
	const OX = 440
	const OY = 80

	var title = Label.new()
	title.text = "ゴール"
	title.position = Vector2(OX + 30, OY - 26)
	add_child(title)

	var num = 1
	for row in range(GRID_SIZE):
		for col in range(GRID_SIZE):
			var panel = ColorRect.new()
			panel.size = Vector2(SMALL, SMALL)
			panel.position = Vector2(OX + col * (SMALL + SM), OY + row * (SMALL + SM))
			if num <= GRID_SIZE * GRID_SIZE - 1:
				panel.color = Color(0.4, 0.6, 1.0)
				var lbl = Label.new()
				lbl.text = str(num)
				lbl.position = Vector2(
					OX + col * (SMALL + SM) + SMALL / 2 - 8,
					OY + row * (SMALL + SM) + SMALL / 2 - 10
				)
				add_child(lbl)
			else:
				panel.color = Color(0.2, 0.2, 0.2)
			add_child(panel)
			num += 1

func on_tile_pressed(tile_pos: Vector2i):
	var diff = tile_pos - empty_pos
	if abs(diff.x) + abs(diff.y) != 1:
		return
	move_tile(tile_pos)
	move_count += 1
	redraw()
	if check_win():
		show_clear()

func check_win() -> bool:
	var num = 1
	for row in range(GRID_SIZE):
		for col in range(GRID_SIZE):
			if row == GRID_SIZE - 1 and col == GRID_SIZE - 1:
				return grid[row][col] == 0
			if grid[row][col] != num:
				return false
			num += 1
	return true

func show_clear():
	var lbl = Label.new()
	lbl.text = "クリア！ %d手で完成！" % move_count
	lbl.position = Vector2(20, GRID_SIZE * (TILE_SIZE + MARGIN) + 80)
	add_child(lbl)
	var btn = Button.new()
	btn.text = "もう一度"
	btn.position = Vector2(20, GRID_SIZE * (TILE_SIZE + MARGIN) + 120)
	btn.pressed.connect(func(): restart())
	add_child(btn)

func restart():
	initialize_grid()
	shuffle_grid()
	redraw()
