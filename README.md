# CoverGemini: Automated LaTeX Resume Pipeline

**CoverGemini** is a mobile-to-cloud automation suite that generates professionally formatted, context-aware engineering CVs in seconds. By bridging iOS Shortcuts, the Gemini 1.5 Flash API, and a custom Flask-based LaTeX compilation server, it transforms a simple job URL into a ready-to-apply PDF.

![CoverGemini Demo](assets/demo.gif)

## 🚀 The Workflow
1. **Extraction**: An iOS Shortcut captures a job offer's HTML and extracts text via JavaScript.
2. **Intelligence**: Content is sent to Gemini 1.5 Flash with a specialized "Service Offer" prompt to generate tailored LaTeX sections (Objectif, Expériences, Compétences).
3. **Transmission**: The Shortcut uses Regex to split the AI response into LaTeX-safe components.
4. **Compilation**: Files are POSTed to a local Flask server that manages a dedicated build directory, handling assets (photo.jpg, logos) and multi-pass `pdflatex` compilation.
5. **Delivery**: The compiled PDF is returned to the iPhone for immediate preview.

## 🛠️ Technical Stack
* **Language**: Python 3.11 (Flask)
* **Typesetting**: TeX Live / MacTeX (pdflatex)
* **Automation**: iOS Shortcuts + JavaScript
* **AI**: Google Gemini API (Flash 1.5)
* **Networking**: ngrok (for local-to-mobile tunneling)

## 🔧 Technical Challenges & Solutions
* **Dynamic Data Partitioning**: Implemented custom Regular Expression (Regex) patterns within iOS Shortcuts to split non-deterministic AI responses into structured LaTeX components.
* **Headless Compilation**: Configured `pdflatex` in `nonstopmode` to handle compilation errors gracefully in a server-side environment.
* **Bilingual Signal Processing**: Optimized the pipeline for bilingual (French/English) technical profiles, ensuring UTF-8 encoding support for engineering terminology.

## 📂 Project Structure
- `app.py`: The Flask server handling multi-part form data and LaTeX sub-processes.
- `template/CV.tex`: The master LaTeX template utilizing `multicols`, `enumitem`, and `fontawesome5`.
- `assets/`: Directory for static branding and demo media.
