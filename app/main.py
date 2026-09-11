from rich.console import Console

console = Console()


def main() -> None:
    console.print(
        "[bold cyan]PersistentCoder v0.1[/bold cyan]"
    )
    console.print("System started successfully.")


if __name__ == "__main__":
    main()