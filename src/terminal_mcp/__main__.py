import uvicorn

from terminal_mcp.config import Settings


def main():
    s = Settings()
    uvicorn.run("terminal_mcp.app:app", host=s.host, port=s.port, reload=False)


if __name__ == "__main__":
    main()
