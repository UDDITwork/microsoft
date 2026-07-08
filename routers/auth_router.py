"""Auth endpoints: register, login, logout."""
from fastapi import APIRouter, Depends, HTTPException, status

import auth
import database
from models import RegisterRequest, LoginRequest, TokenResponse

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register", response_model=TokenResponse)
async def register(req: RegisterRequest, conn: database.Connection = Depends(database.get_conn)):
    cur = await conn.execute("SELECT id FROM users WHERE username = ?", (req.username,))
    if await cur.fetchone():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already taken")

    pw_hash = auth.hash_password(req.password)
    cur = await conn.execute(
        "INSERT INTO users (username, password_hash) VALUES (?, ?)",
        (req.username, pw_hash),
    )
    await conn.commit()
    user_id = cur.lastrowid
    token = auth.create_token(user_id, req.username)
    return TokenResponse(access_token=token, username=req.username, user_id=user_id)


@router.post("/login", response_model=TokenResponse)
async def login(req: LoginRequest, conn: database.Connection = Depends(database.get_conn)):
    cur = await conn.execute(
        "SELECT id, password_hash FROM users WHERE username = ?", (req.username,)
    )
    row = await cur.fetchone()
    if not row or not auth.verify_password(req.password, row["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password"
        )
    token = auth.create_token(row["id"], req.username)
    return TokenResponse(access_token=token, username=req.username, user_id=row["id"])


@router.post("/logout")
async def logout(user: dict = Depends(auth.get_current_user)):
    # Stateless JWT: logout is client-side (drop the token). Endpoint exists for
    # symmetry and future token-revocation support.
    return {"status": "logged_out"}
