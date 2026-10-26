PDF Accessibility Studio
========================

WHAT THIS IS
  A local tool for making PDFs accessible. It runs entirely on this computer: nothing is uploaded and no
  cloud or AI service is used. Your original files are never changed.

INSTALL
  Copy this whole folder anywhere (for example C:\Tools\PDF Accessibility Studio, or a shared drive) and
  run it from there. There is nothing to install and no administrator rights are needed.
  Keep the "tools" folder next to the programs: it holds Java and the veraPDF validator.
  If Windows warns about an unknown program the first time, or your IT department blocks programs from this
  folder, ask them to allow it: the programs are not digitally signed.

TWO WAYS TO USE IT (same engine, same results)

  1. In your browser (recommended)
       Double-click  "PDF Accessibility Studio (browser).exe"
     Your web browser opens with the tool. There is no window to keep open: when you close the last browser tab
     the tool stops by itself after about 10 minutes (your work is saved). Use "Quit" in the page to stop it at once.
     Starting it again re-opens the copy that is already running.

     Library    Add PDFs, or a whole folder. Each is made accessible automatically. Filter by what each still needs,
                select many, and Export them in one go (a zip with the accessible PDFs in their folders plus a report).
     Images     Write a description for each meaningful image, or mark it Decorative. Ctrl+Enter saves and moves on.
     Pages      Review and correct the tags: reading order, headings, tables. Everything can be undone.
     Document   Check the title and language the tool chose, and the things it wants you to confirm.
     Result     See whether the file passes the automated PDF/UA-1 check (veraPDF), then download it.

  2. As a desktop window
       Double-click  "PDF Accessibility Studio.exe"
     The same steps, in an ordinary program window, for one file at a time.

PROCESS A WHOLE FOLDER FROM A TERMINAL
  Open a terminal in this folder and run:
    pdfa11y.exe batch "C:\path\to\pdfs" --out "C:\path\to\accessible"
  then open  accessible\_report\batch.html
  Other commands:  pdfa11y.exe --help

CHECK THE INSTALLATION
    pdfa11y.exe selftest
  Every line should say [OK]. If one says FAIL, send that line to whoever looks after the tool.

GOOD TO KNOW
  * "Passes PDF/UA-1 check (veraPDF)" means an independent validator found no rule violations. It cannot
    judge whether image descriptions are meaningful, whether the reading order makes sense, or whether
    tables are sensible: a person should still look over the result.
  * Work in progress is stored in  %LOCALAPPDATA%\pdfa11y   (the browser version keeps it in the "web" folder there)
  * If something goes wrong, details are written to  %LOCALAPPDATA%\pdfa11y\web.log  (browser version) or
    %LOCALAPPDATA%\pdfa11y\crash.log  (desktop window).
  * The tool listens only on your own computer and needs the one-time key in its address; other computers
    and other websites cannot reach it.

Third-party components and their licences: see THIRD-PARTY-NOTICES.txt
