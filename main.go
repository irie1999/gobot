package main

import (
	"fmt"
	"math/rand"
	"os"
	"os/signal"
	"syscall"
	"time"

	"golang.org/x/term"
)

const (
	width  = 40
	height = 20
)

type Point struct {
	x, y int
}

type Direction int

const (
	Up Direction = iota
	Down
	Left
	Right
)

type Game struct {
	snake     []Point
	food      Point
	dir       Direction
	score     int
	over      bool
	oldState  *term.State
}

func NewGame() *Game {
	snake := []Point{{width / 2, height / 2}, {width/2 - 1, height / 2}}
	g := &Game{
		snake: snake,
		dir:   Right,
	}
	g.placeFood()
	return g
}

func (g *Game) placeFood() {
	for {
		f := Point{rand.Intn(width-2) + 1, rand.Intn(height-2) + 1}
		collision := false
		for _, s := range g.snake {
			if s == f {
				collision = true
				break
			}
		}
		if !collision {
			g.food = f
			return
		}
	}
}

func (g *Game) Update() {
	if g.over {
		return
	}
	head := g.snake[0]
	var next Point
	switch g.dir {
	case Up:
		next = Point{head.x, head.y - 1}
	case Down:
		next = Point{head.x, head.y + 1}
	case Left:
		next = Point{head.x - 1, head.y}
	case Right:
		next = Point{head.x + 1, head.y}
	}

	// Wall collision
	if next.x <= 0 || next.x >= width-1 || next.y <= 0 || next.y >= height-1 {
		g.over = true
		return
	}

	// Self collision
	for _, s := range g.snake {
		if s == next {
			g.over = true
			return
		}
	}

	g.snake = append([]Point{next}, g.snake...)
	if next == g.food {
		g.score++
		g.placeFood()
	} else {
		g.snake = g.snake[:len(g.snake)-1]
	}
}

func (g *Game) Render() {
	board := make([][]rune, height)
	for i := range board {
		board[i] = make([]rune, width)
		for j := range board[i] {
			board[i][j] = ' '
		}
	}

	// Draw walls
	for x := 0; x < width; x++ {
		board[0][x] = '#'
		board[height-1][x] = '#'
	}
	for y := 0; y < height; y++ {
		board[y][0] = '#'
		board[y][width-1] = '#'
	}

	// Draw food
	board[g.food.y][g.food.x] = '*'

	// Draw snake
	for i, s := range g.snake {
		if i == 0 {
			board[s.y][s.x] = 'O'
		} else {
			board[s.y][s.x] = 'o'
		}
	}

	// Clear screen and move cursor to top
	fmt.Print("\033[H\033[2J")
	fmt.Printf("  Snake Game  |  Score: %d  |  Length: %d\n\n", g.score, len(g.snake))
	for _, row := range board {
		fmt.Println(string(row))
	}
	fmt.Println("\n  WASD or Arrow keys to move  |  Q to quit")
}

func (g *Game) SetDir(d Direction) {
	// Prevent reversing direction
	if g.dir == Up && d == Down {
		return
	}
	if g.dir == Down && d == Up {
		return
	}
	if g.dir == Left && d == Right {
		return
	}
	if g.dir == Right && d == Left {
		return
	}
	g.dir = d
}

func main() {
	rand.Seed(time.Now().UnixNano())

	oldState, err := term.MakeRaw(int(os.Stdin.Fd()))
	if err != nil {
		fmt.Println("Error setting terminal:", err)
		os.Exit(1)
	}
	defer term.Restore(int(os.Stdin.Fd()), oldState)

	// Handle Ctrl+C
	sigs := make(chan os.Signal, 1)
	signal.Notify(sigs, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		<-sigs
		term.Restore(int(os.Stdin.Fd()), oldState)
		fmt.Print("\033[H\033[2J")
		os.Exit(0)
	}()

	game := NewGame()
	input := make(chan byte, 10)

	go func() {
		buf := make([]byte, 3)
		for {
			n, _ := os.Stdin.Read(buf)
			if n > 0 {
				input <- buf[0]
				if n == 3 {
					// Arrow key sequence: ESC [ A/B/C/D
					input <- buf[1]
					input <- buf[2]
				}
			}
		}
	}()

	ticker := time.NewTicker(150 * time.Millisecond)
	defer ticker.Stop()

	game.Render()

	arrowSeq := false
	bracketSeen := false

	for {
		select {
		case b := <-input:
			if arrowSeq && bracketSeen {
				arrowSeq = false
				bracketSeen = false
				switch b {
				case 'A':
					game.SetDir(Up)
				case 'B':
					game.SetDir(Down)
				case 'C':
					game.SetDir(Right)
				case 'D':
					game.SetDir(Left)
				}
			} else if arrowSeq {
				if b == '[' {
					bracketSeen = true
				} else {
					arrowSeq = false
				}
			} else {
				switch b {
				case 27: // ESC
					arrowSeq = true
				case 'w', 'W':
					game.SetDir(Up)
				case 's', 'S':
					game.SetDir(Down)
				case 'a', 'A':
					game.SetDir(Left)
				case 'd', 'D':
					game.SetDir(Right)
				case 'q', 'Q', 3: // Q or Ctrl+C
					term.Restore(int(os.Stdin.Fd()), oldState)
					fmt.Print("\033[H\033[2J")
					fmt.Printf("Game Over! Final Score: %d | Length: %d\n", game.score, len(game.snake))
					os.Exit(0)
				}
			}
		case <-ticker.C:
			game.Update()
			game.Render()
			if game.over {
				fmt.Println("\n  *** GAME OVER ***")
				fmt.Printf("  Final Score: %d | Length: %d\n", game.score, len(game.snake))
				fmt.Println("\n  Press Q to quit")
				// Wait for quit
				for b := range input {
					if b == 'q' || b == 'Q' || b == 3 {
						term.Restore(int(os.Stdin.Fd()), oldState)
						fmt.Print("\033[H\033[2J")
						os.Exit(0)
					}
				}
			}
		}
	}
}
