@echo off
rem Publie le projet sur GitHub : double-clic, ou "publier.cmd mon message".
setlocal EnableDelayedExpansion
cd /d "%~dp0"

where git >nul 2>nul || (echo Git n'est pas installe : https://git-scm.com/download/win & goto :fin)

rem Identite Git (une seule fois sur ce PC)
git config user.email >nul 2>nul
if errorlevel 1 (
  set /p GNAME=Ton nom pour Git :
  set /p GEMAIL=Ton email GitHub :
  git config --global user.name "!GNAME!"
  git config --global user.email "!GEMAIL!"
)

rem Premiere fois : creation du depot local et lien avec GitHub
if not exist .git (
  set /p REPO=URL du depot GitHub ^(ex. https://github.com/pseudo/music-radar.git^) :
  git init -b main || goto :erreur
  git remote add origin !REPO! || goto :erreur
)

git add -A || goto :erreur

rem Garde-fou : jamais de secrets ni de donnees personnelles sur GitHub
for %%F in (.env artists.txt) do (
  git diff --cached --name-only | findstr /x /c:"%%F" >nul && (
    echo STOP : %%F allait etre publie. Rien n'a ete envoye.
    git reset -q
    goto :fin
  )
)

git diff --cached --quiet && (echo Rien de nouveau a publier. & goto :fin)

set "MSG=%*"
if "!MSG!"=="" set "MSG=Mise a jour"
git commit -q -m "!MSG!" || goto :erreur
git push -u origin main || goto :erreur
echo.
echo Publie sur GitHub. Sur le serveur : bash ~/music-radar/deploy.sh
goto :fin

:erreur
echo.
echo Erreur : regarde le message ci-dessus.

:fin
echo.
pause
