from src.swarm.cli import main

# The guard matters: `work` starts worker processes with the `spawn` method, and each child
# imports this module again.
if __name__ == "__main__":
    raise SystemExit(main())
