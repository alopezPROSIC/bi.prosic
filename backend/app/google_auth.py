"""Verificación del ID token de «Iniciar sesión con Google» (Google Identity Services).

Se valida firma (llaves públicas de Google), audiencia (nuestro Client ID), emisor, expiración, correo verificado y
que la cuenta pertenezca al dominio de Google Workspace permitido (claim ``hd`` + sufijo del correo).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import lru_cache

import jwt

GOOGLE_CERTS = "https://www.googleapis.com/oauth2/v3/certs"
ISSUERS = ("accounts.google.com", "https://accounts.google.com")


class GoogleAuthError(Exception):
    pass


@dataclass(frozen=True)
class GoogleIdentity:
    email: str
    name: str
    domain: str


@lru_cache(maxsize=1)
def _jwks() -> jwt.PyJWKClient:
    return jwt.PyJWKClient(GOOGLE_CERTS, cache_keys=True, lifespan=3600)


def _verify(credential: str, client_id: str, domain: str) -> GoogleIdentity:
    try:
        key = _jwks().get_signing_key_from_jwt(credential)
        claims = jwt.decode(credential, key.key, algorithms=["RS256"], audience=client_id,
                            options={"require": ["exp", "iat", "iss", "aud", "email"]})
    except jwt.PyJWTError as exc:
        raise GoogleAuthError(f"Token de Google inválido: {exc}") from exc
    if claims.get("iss") not in ISSUERS:
        raise GoogleAuthError("Emisor del token inválido")
    email = str(claims.get("email", "")).lower()
    if not claims.get("email_verified"):
        raise GoogleAuthError("El correo de Google no está verificado")
    dom = domain.lower().lstrip("@")
    # hd solo existe en cuentas de Google Workspace: descarta cuentas personales (@gmail.com) con el mismo correo.
    if claims.get("hd", "").lower() != dom or not email.endswith("@" + dom):
        raise GoogleAuthError(f"Solo se permiten cuentas de Google @{dom}")
    return GoogleIdentity(email=email, name=str(claims.get("name") or email), domain=dom)


async def verify_google_credential(credential: str, client_id: str, domain: str) -> GoogleIdentity:
    if not client_id:
        raise GoogleAuthError("El inicio de sesión con Google no está configurado (GOOGLE_CLIENT_ID)")
    return await asyncio.to_thread(_verify, credential, client_id, domain)
