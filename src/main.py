"""Orion — 启动入口"""

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "server:app",
        host="127.0.0.1",
        port=8080,
        reload=True
    )
