from pydantic import BaseModel


class JDTextInput(BaseModel):
    text: str


class JDUploadResponse(BaseModel):
    jd_text: str
    word_count: int
    char_count: int
