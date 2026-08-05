import os
from cryptography.fernet import Fernet
from dotenv import load_dotenv

load_dotenv()

ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")

if not ENCRYPTION_KEY:
    raise RuntimeError(
        "ENCRYPTION_KEY is not set in .env. "
    )

fernet = Fernet(ENCRYPTION_KEY.encode())

def encrypt_password(plain_password: str) -> str:
    return fernet.encrypt(plain_password.encode()).decode()


def decrypt_password(encrypted_password: str) -> str:
    return fernet.decrypt(encrypted_password.encode()).decode()