from app.config import get_settings
import uvicorn

if __name__ == "__main__":
    s = get_settings()
    # HOST e validado em config.py: so 127.0.0.1/localhost
    uvicorn.run("app.main:app", host=s.host, port=s.port, reload=False, proxy_headers=False)
