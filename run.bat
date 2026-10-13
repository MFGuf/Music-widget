@echo off
rem Запуск виджета VK Музыки.
rem
rem Скрипт проверяет, что всё на месте, и только потом запускает виджет.
rem Иначе ошибка в зависимостях выглядит как пустое окно, и непонятно,
rem что чинить.

setlocal
cd /d "%~dp0"

set "PY="
rem Ищем интерпретатор: сначала тот, что прописан в системе, потом те,
rem где его обычно ставят. Без этого PATH должен содержать python.
where pythonw >nul 2>&1 && set "PY=pythonw"
if not defined PY (
    for %%I in (
        "%LOCALAPPDATA%\Programs\Python\Python314\pythonw.exe"
        "%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe"
        "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"
        "%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe"
        "%LOCALAPPDATA%\Programs\Python\Python310\pythonw.exe"
        "C:\Python314\pythonw.exe"
        "C:\Python313\pythonw.exe"
        "C:\Python312\pythonw.exe"
    ) do (
        if exist %%I (
            set "PY=%%~I"
            goto found
        )
    )
)

:found
if not defined PY (
    echo Не найден Python. Установите Python 3.10 или новее.
    pause
    exit /b 1
)

rem pillow нужен для обоев и обложек. Без него виджет не запустится вовсе,
rem и проверить это надо до старта, а не после.
"%PY%" -c "import PIL" >nul 2>&1
if errorlevel 1 (
    echo Не хватает pillow. Установите: python -m pip install -r requirements.txt
    pause
    exit /b 1
)

rem Хост native messaging должен быть зарегистрирован, иначе браузер не
rem сможет до нас достучаться. Это самая частая причина "виджет молчит".
rem Ищем именно в реестре: туда пишет install_host.ps1 и оттуда читает
rem Firefox. Файла в профиле Mozilla искать не надо.
reg query "HKCU\Software\Mozilla\NativeMessagingHosts\music_widget_host" >nul 2>&1
if errorlevel 1 (
    echo Внимание: хост не зарегистрирован, Firefox до виджета не дотянется.
    echo Запустите один раз: powershell -ExecutionPolicy Bypass -File native\install_host.ps1
    echo.
)

echo Запускаю виджет...
start "" "%PY%" "%~dp0widget\vk_music_widget.py"

endlocal