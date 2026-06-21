import chess
import chess.engine

def main():
    # Start the Stockfish engine (installed via apt)
    engine = chess.engine.SimpleEngine.popen_uci(["stockfish"])

    # Initial chess position (normal starting board)
    board = chess.Board()

    print("Initial position FEN:")
    print(board.fen())
    print()

    # Ask Stockfish for a move, think for 0.5 seconds
    result = engine.play(board, chess.engine.Limit(time=0.5))

    print("Stockfish suggests move:", result.move)           # e.g. e2e4
    print("In algebraic notation:", board.san(result.move))  # e.g. e4

    engine.quit()

if __name__ == "__main__":
    main()
