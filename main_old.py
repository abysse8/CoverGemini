from typing import Any, Dict, List


def latex_escape(value: Any) -> str:
    text = "" if value is None else str(value)

    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    return text


def latex_join(values: List[str]) -> str:
    clean_values = [str(value).strip() for value in values if str(value).strip()]
    return latex_escape(", ".join(clean_values))


def build_objectif_block(data: Dict[str, Any]) -> str:
    objective = latex_escape(data.get("objective", ""))
    if not objective:
        return ""

    return f"""\\section*{{Objectif}}
{objective}""".strip()


def build_apl_block(data: Dict[str, Any]) -> str:
    apl_items = data.get("apl_items", [])[:3]
    apl_tex = "\n".join(
        f"    \\item {latex_escape(item)}" for item in apl_items
    )

    return f"""\\textbf{{Research Intern – RISE@APL (Johns Hopkins APL)}} \\hfill \\textit{{2024 – 2025}}
\\begin{{itemize}}[leftmargin=*]
{apl_tex}
\\end{{itemize}}""".strip()


def build_competences_block(data: Dict[str, Any]) -> str:
    skills = data.get("skills", {})
    languages = skills.get("languages", [])
    embedded = skills.get("embedded", [])
    tools = skills.get("tools", [])

    lines = ["\\section*{Compétences}"]

    if languages:
        lines.append(f"\\textbf{{Langages}} : {latex_join(languages)} \\\\")

    if embedded:
        lines.append(f"\\textbf{{Systèmes embarqués}} : {latex_join(embedded)} \\\\")

    if tools:
        lines.append(f"\\textbf{{Outils}} : {latex_join(tools)}")

    return "\n".join(lines)


def build_latex_patch(data: Dict[str, Any], company: str = "", role_title: str = "") -> str:
    return "\n\n".join([
        "% ===== COVERAI PATCH =====",
        f"% Company: {latex_escape(company or 'Entreprise')}",
        f"% Role: {latex_escape(role_title or 'Poste')}",
        build_objectif_block(data),
        build_apl_block(data),
        build_competences_block(data),
        "% LETTRE",
        f"% {latex_escape(data.get('letter', ''))}",
    ]).strip()


def render_template(template: str, data: Dict[str, Any], company: str = "", role_title: str = "") -> str:
    rendered = template
    rendered = rendered.replace("$$$OBJECTIF$$$", build_objectif_block(data))
    rendered = rendered.replace("$$$APL$$$", build_apl_block(data))
    rendered = rendered.replace("$$$COMPETENCES$$$", build_competences_block(data))
    rendered = rendered.replace("{{COVERAI_PATCH}}", build_latex_patch(data, company, role_title))
    return rendered


def build_cv_tex(data: Dict[str, Any], company: str = "", role_title: str = "", use_full_template: bool = True) -> str:
    template = FULL_CV_TEMPLATE if use_full_template else MINIMAL_CV_TEMPLATE
    return render_template(template, data, company, role_title)


MINIMAL_CV_TEMPLATE = r"""
\documentclass[11pt,a4paper]{article}
\usepackage[utf8]{inputenc}
\usepackage[T1]{fontenc}
\usepackage[french]{babel}
\usepackage[margin=1.8cm]{geometry}
\usepackage{enumitem}

\newcommand{\cvline}[2]{\textbf{#1} : #2\par}

\begin{document}
{{COVERAI_PATCH}}
\end{document}
""".strip()


FULL_CV_TEMPLATE = r"""
\documentclass[10pt,a4paper]{article}
\usepackage[utf8]{inputenc}
\usepackage[T1]{fontenc}
\usepackage[french]{babel}
\usepackage{geometry}
\geometry{left=1cm, right=1cm, top=1cm, bottom=1cm}
\usepackage{enumitem}
\usepackage{hyperref}
\usepackage{fontawesome5}
\usepackage{multicol}
\usepackage{graphicx}
\setlength{\columnsep}{0.6cm}
\setlist{nosep, left=0pt, itemsep=0pt, topsep=0pt, partopsep=0pt, parsep=0pt}
\pagestyle{empty}

\begin{document}

% ========== HEADER ==========
\begin{minipage}{0.2\textwidth}
    \includegraphics[width=\linewidth]{photo.jpg}
\end{minipage}
\begin{minipage}{0.6\textwidth}
    \centering
    {\huge \textbf{Julien GONZALES}} \\[0.2cm]
    \textbf{Apprenti Ingénieur en Systèmes Électriques et Électroniques Embarqués}\\
    \textit{Recherche contrat d'apprentissage de 36 mois}\\[0.1cm]
\end{minipage}
\begin{minipage}{0.2\textwidth}
    \raggedleft
    \includegraphics[width=2.5cm]{logo_cefipa.png}\\[0.2cm]
    \includegraphics[width=2.5cm]{logo_cesi.png}
\end{minipage}

\vspace{0.2cm}

% ========== CONTACT INFO ==========
\noindent
\faEnvelope\ \href{mailto:julienabdougonzales@gmail.com}{julienabdougonzales@gmail.com} \quad
\faPhone\ 07.75.85.70.82 \quad
\faMapMarker\ 91400 Orsay / Mobilité France entière \quad
\faCar\ Permis B

\vspace{0.3cm}

\begin{multicols}{2}

$$$OBJECTIF$$$

% ========== FORMATION ==========
\section*{Formation}
\textbf{Programme ingénieur S3E (Bac+5) – CESI Nanterre} \hfill \textit{2026 – 2029} \\
Systèmes embarqués, IA temps réel, FPGA, RTOS.

\textbf{Cursus génie électrique (Bac+3) – Johns Hopkins University} \hfill \textit{2022 – 2025} \\
Mention très bien – GPA 3,44/4,0. Spécialisation : systèmes embarqués, traitement du signal, capteurs.


% ========== EXPÉRIENCES PROFESSIONNELLES ==========
\section*{Expériences professionnelles}
\textbf{Câbleur – Electro Faisceaux} \hfill \textit{depuis janvier 2026}
\begin{itemize}[leftmargin=*]
    \item \textbf{Assemble et intègre} des faisceaux électriques complexes pour systèmes industriels et dispositifs médicaux critiques.
    \item \textbf{Diagnostique, corrige et valide} les défauts électriques en environnement normé (traçabilité, qualité, conformité).
\end{itemize}

$$$APL$$$

\textbf{Assistant laboratoire – Johns Hopkins University} \hfill \textit{2024 – 2025}
\begin{itemize}[leftmargin=*]
    \item \textbf{Conçoit et exécute} des protocoles de validation pour systèmes électroniques de précision.
    \item \textbf{Structure et formalise} la documentation technique pour reproductibilité expérimentale.
\end{itemize}

% ========== PROJETS ==========
\section*{Projets – Réalisations concrètes}
\textbf{CNN pour reconnaissance d’images – TensorFlow/PyTorch} \hfill \textit{2024 – 2025}
\begin{itemize}[leftmargin=*]
    \item \textbf{Développe et entraîne} des architectures CNN pour classification (CIFAR-10, MNIST).
    \item \textbf{Réduit la latence et l’empreinte mémoire} via quantification et optimisation embarquée.
\end{itemize}

\textbf{Ondelettes sur FPGA (DWT)} \hfill \textit{2024}
\begin{itemize}[leftmargin=*]
    \item \textbf{Implémente et pipeline} une DWT sur FPGA (VHDL) pour traitement temps réel.
    \item \textbf{Démontre un gain de performance} sur le débruitage d’images infrarouges.
\end{itemize}

\textbf{Drone – Fusion de capteurs \& PID} \hfill \textit{2024 – 2025}
\begin{itemize}[leftmargin=*]
    \item \textbf{Développe un firmware embarqué robuste} (STM32, C) intégrant IMU (MPU6050) via I2C.
    \item \textbf{Implémente une fusion de capteurs temps réel} et une boucle PID stable (<10 ms).
\end{itemize}

\textbf{Oxymètre – Pipeline Linux embarqué} \hfill \textit{2024}
\begin{itemize}[leftmargin=*]
    \item \textbf{Construit une chaîne complète} d’acquisition et traitement (SPI, filtrage, calcul SpO2).
    \item \textbf{Industrialise les tests} via scripts Python/Bash et gestion de version Git.
\end{itemize}

% ========== RECHERCHE – CSMS LAB ==========
\section*{Recherche – CSMS Lab (Johns Hopkins)}
\textit{3 ans – apprentissage alternatif, systèmes haute dimension}
\begin{itemize}[leftmargin=*]
    \item \textbf{Conçoit des algorithmes d’apprentissage locaux} comme alternative scalable à la rétropropagation.
    \item \textbf{Explore les propriétés des systèmes en grande dimension}.
    \item \textbf{Relie théorie et systèmes physiques} (neuromorphique, calcul distribué, capteurs intelligents).
\end{itemize}

\end{multicols}

\vspace{0.1cm}
$$$COMPETENCES$$$
\vspace{0.2cm}

% ========== LANGUES & CENTRES D'INTÉRÊT ==========
\noindent
\textbf{Langues} : Français (natif), Anglais (bilingue – TOEIC 950) \hfill
\textbf{Centres d’intérêt} : Course à pied, photographie, veille technologique.

\end{document}
""".strip()