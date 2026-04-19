from flask import Flask, request, send_file, jsonify
import subprocess, os, uuid, datetime

app = Flask(__name__)
WORKDIR = "jobs"
LOG_FILE = "server_debug.log"
# Update this to your local pdflatex path (e.g., /Library/TeX/texbin/pdflatex)
PDFLATEX_PATH = "pdflatex" 

os.makedirs(WORKDIR, exist_ok=True)

def log(message):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    formatted = f"[{timestamp}] {message}"
    with open(LOG_FILE, "a") as f:
        f.write(formatted + "\n")

@app.route("/compile", methods=["POST"])
def compile_tex():
    job_id = str(uuid.uuid4())
    job_dir = os.path.join(WORKDIR, job_id)
    os.makedirs(job_dir)
    
    try:
        if not request.files:
            return jsonify({"error": "No files received"}), 400

        for key in request.files:
            for file in request.files.getlist(key):
                file.save(os.path.join(job_dir, file.filename))

        tex_filename = "CV.tex"
        cmd = [PDFLATEX_PATH, "-interaction=nonstopmode", tex_filename]
        
        # Double pass for references
        subprocess.run(cmd, cwd=job_dir, capture_output=True)
        subprocess.run(cmd, cwd=job_dir, capture_output=True)

        pdf_path = os.path.join(job_dir, "CV.pdf")
        if os.path.exists(pdf_path):
            return send_file(pdf_path, mimetype='application/pdf')
        return jsonify({"error": "Compilation failed"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
