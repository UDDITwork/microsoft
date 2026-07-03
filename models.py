"""Pydantic request/response models."""
from typing import Optional, List
from pydantic import BaseModel, Field


# --- Auth --------------------------------------------------------------------

class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=6, max_length=256)


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str
    user_id: int


# --- Sessions ----------------------------------------------------------------

class CreateSessionRequest(BaseModel):
    title: Optional[str] = None


class SessionResponse(BaseModel):
    id: str
    title: str
    created_at: str
    updated_at: str
    status: str
    extraction_status: str


# --- Chat --------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str
    # Optional: set when a section button is clicked so routing is explicit.
    section_type: Optional[str] = None


class MessageResponse(BaseModel):
    id: int
    role: str
    content: str
    metadata: Optional[str] = None
    created_at: str


# --- Prompts (admin) ---------------------------------------------------------

class UpdatePromptRequest(BaseModel):
    system_prompt: str


class PromptResponse(BaseModel):
    section_type: str
    system_prompt: str
    updated_at: str


# --- Documents / extraction --------------------------------------------------

class DocumentResponse(BaseModel):
    id: int
    filename: str
    doc_type: Optional[str]
    uploaded_at: str


class ExtractedContentResponse(BaseModel):
    id: int
    content_type: str
    content_text: str
    claim_number: Optional[int]
    metadata: Optional[str]


class SectionResponse(BaseModel):
    id: int
    section_type: str
    version: int
    content: str
    generated_at: str
