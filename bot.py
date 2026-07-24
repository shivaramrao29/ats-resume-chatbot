import os
import json
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler
from openai import OpenAI
from pypdf import PdfReader
from docx import Document
import io

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

OPENROUTER_MODEL = "deepseek/deepseek-r1:free"  # free model, no credits needed - change here to try others

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.environ.get("OPENROUTER_API_KEY"),
)

WAITING_FOR_JD, WAITING_FOR_RESUME, WAITING_FOR_ACTION = range(3)

user_sessions = {}

class ResumeAISession:
    def __init__(self):
        self.jd = None
        self.resumes = {}
        self.scores = {}
    
    def reset(self):
        self.__init__()

def extract_pdf_text(pdf_file):
    try:
        reader = PdfReader(io.BytesIO(pdf_file))
        text = ""
        for page in reader.pages:
            text += page.extract_text()
        return text
    except Exception as e:
        logger.error(f"PDF extraction error: {e}")
        return None

def extract_docx_text(docx_file):
    try:
        doc = Document(io.BytesIO(docx_file))
        text = ""
        for para in doc.paragraphs:
            text += para.text + "\n"
        return text
    except Exception as e:
        logger.error(f"DOCX extraction error: {e}")
        return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_sessions[user_id] = ResumeAISession()
    
    await update.message.reply_text(
        "👋 Welcome to ATS Resume AI Bot!\n\n"
        "I'll help you analyze and improve your resumes.\n\n"
        "📋 First, send me the Job Description (JD) as text or paste it.\n\n"
        "Commands:\n"
        "/reset - Start over\n"
        "/help - Show help"
    )
    return WAITING_FOR_JD

async def handle_jd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = user_sessions.get(user_id)
    
    if not session:
        session = ResumeAISession()
        user_sessions[user_id] = session
    
    if update.message.text:
        session.jd = update.message.text
        await update.message.reply_text(
            "✅ Job Description received!\n\n"
            "Now, send me your resume(s) as PDF or DOCX files.\n"
            "You can send multiple resumes one by one.\n\n"
            "Type /done when you've sent all resumes."
        )
        return WAITING_FOR_RESUME
    
    elif update.message.document:
        file = await update.message.effective_attachment.get_file()
        file_bytes = await file.download_as_bytearray()
        file_name = update.message.document.file_name.lower()
        
        if file_name.endswith('.pdf'):
            text = extract_pdf_text(file_bytes)
        elif file_name.endswith('.docx'):
            text = extract_docx_text(file_bytes)
        else:
            await update.message.reply_text("❌ Please send PDF or DOCX file for JD")
            return WAITING_FOR_JD
        
        if text:
            session.jd = text
            await update.message.reply_text(
                "✅ Job Description received!\n\n"
                "Now, send me your resume(s) as PDF or DOCX files.\n"
                "You can send multiple resumes one by one.\n\n"
                "Type /done when you've sent all resumes."
            )
            return WAITING_FOR_RESUME
        else:
            await update.message.reply_text("❌ Failed to extract text from JD file")
            return WAITING_FOR_JD

async def handle_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = user_sessions.get(user_id)
    
    if not session or not session.jd:
        await update.message.reply_text("❌ Please send JD first using /start")
        return WAITING_FOR_JD
    
    if update.message.document:
        file = await update.message.effective_attachment.get_file()
        file_bytes = await file.download_as_bytearray()
        file_name = update.message.document.file_name.lower()
        
        if file_name.endswith('.pdf'):
            resume_text = extract_pdf_text(file_bytes)
        elif file_name.endswith('.docx'):
            resume_text = extract_docx_text(file_bytes)
        else:
            await update.message.reply_text("❌ Please send PDF or DOCX resume")
            return WAITING_FOR_RESUME
        
        if not resume_text:
            await update.message.reply_text("❌ Failed to extract text from resume")
            return WAITING_FOR_RESUME
        
        resume_name = file_name.replace('.pdf', '').replace('.docx', '')
        session.resumes[resume_name] = resume_text
        
        await update.message.reply_text(f"📄 Resume '{resume_name}' received. Analyzing...\n\n⏳ This may take 30 seconds...")
        
        try:
            analysis = await analyze_resume_with_claude(session.jd, resume_text, resume_name)
            session.scores[resume_name] = analysis
            
            response = f"📊 **Analysis for {resume_name}**\n\n"
            response += f"🎯 **ATS Score**: {analysis['ats_score']}/100\n\n"
            response += f"**Strengths:**\n{analysis['strengths']}\n\n"
            response += f"**Gaps & Missing Keywords:**\n{analysis['gaps']}\n\n"
            response += f"**Suggestions for Improvement:**\n{analysis['suggestions']}\n\n"
            
            await update.message.reply_text(response, parse_mode='Markdown')
            
            await update.message.reply_text(
                "📝 Options:\n"
                "/modify " + resume_name + " - Get a modified version\n"
                "Send another resume or /analyze to see all scores"
            )
        except Exception as e:
            logger.error(f"Claude API error: {e}")
            await update.message.reply_text(f"❌ Analysis failed: {str(e)}")
        
        return WAITING_FOR_RESUME
    
    await update.message.reply_text("❌ Please send a resume file")
    return WAITING_FOR_RESUME

async def analyze_resume_with_claude(jd, resume, resume_name):
    prompt = f"""You are an ATS (Applicant Tracking System) expert and resume optimizer.

Analyze the following resume against the job description and provide:
1. ATS Score (0-100)
2. Top 3 Strengths (what matches well)
3. Critical Gaps (missing keywords, skills, experience)
4. Top 5 Suggestions for improvement

Format your response as JSON with keys: ats_score, strengths, gaps, suggestions

JOB DESCRIPTION:
{jd}

RESUME:
{resume}

Provide ONLY valid JSON, no markdown or extra text."""
    
    response = client.chat.completions.create(
        model=OPENROUTER_MODEL,
        max_tokens=1500,
        messages=[
            {"role": "user", "content": prompt}
        ]
    )
    
    response_text = response.choices[0].message.content
    
    try:
        analysis = json.loads(response_text)
    except:
        analysis = {
            "ats_score": 65,
            "strengths": "Unable to parse response",
            "gaps": "Please retry",
            "suggestions": "Technical issue occurred"
        }
    
    return analysis

async def modify_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = user_sessions.get(user_id)
    
    if not session:
        await update.message.reply_text("❌ No session found. Use /start")
        return WAITING_FOR_RESUME
    
    args = update.message.text.split()
    if len(args) < 2:
        await update.message.reply_text("❌ Usage: /modify resume_name")
        return WAITING_FOR_RESUME
    
    resume_name = args[1]
    
    if resume_name not in session.resumes:
        await update.message.reply_text(f"❌ Resume '{resume_name}' not found")
        return WAITING_FOR_RESUME
    
    await update.message.reply_text(f"✨ Regenerating resume '{resume_name}'...\n⏳ Processing...")
    
    try:
        modified_resume = await regenerate_resume_with_claude(
            session.jd,
            session.resumes[resume_name],
            session.scores[resume_name]['suggestions']
        )
        
        modified_name = f"{resume_name}_modified"
        session.resumes[modified_name] = modified_resume
        
        await update.message.reply_text(
            f"✅ **Modified Resume for {resume_name}**\n\n"
            f"```\n{modified_resume[:3000]}\n...\n```\n\n"
            f"(Full resume saved in system - request export for complete version)"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Modification failed: {str(e)}")
    
    return WAITING_FOR_RESUME

async def regenerate_resume_with_claude(jd, resume, suggestions):
    prompt = f"""You are an expert resume writer. Rewrite the following resume to better match the job description.

Apply these specific improvements:
{suggestions}

Keep the resume factually accurate and realistic. Maintain actual experience but improve formatting, keyword optimization, and impact.

Original Resume:
{resume}

Job Description:
{jd}

Provide the complete rewritten resume. Do not add commentary, just the resume text."""
    
    response = client.chat.completions.create(
        model=OPENROUTER_MODEL,
        max_tokens=2000,
        messages=[
            {"role": "user", "content": prompt}
        ]
    )
    
    return response.choices[0].message.content

async def show_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = user_sessions.get(user_id)
    
    if not session or not session.scores:
        await update.message.reply_text("❌ No resumes analyzed yet")
        return WAITING_FOR_RESUME
    
    response = "📊 **Resume Analysis Summary**\n\n"
    for resume_name, score_data in session.scores.items():
        response += f"• {resume_name}: {score_data['ats_score']}/100\n"
    
    await update.message.reply_text(response, parse_mode='Markdown')
    return WAITING_FOR_RESUME

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_sessions[user_id] = ResumeAISession()
    await update.message.reply_text("🔄 Session reset! Use /start to begin again.")
    return WAITING_FOR_JD

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
🤖 **ATS Resume AI Bot - Help**

**How to use:**
1. /start - Initialize bot
2. Send JD as text or PDF/DOCX file
3. Send resume(s) as PDF/DOCX
4. Bot analyzes and scores each resume
5. /modify resume_name - Get improved version
6. /analyze - View all scores
7. /reset - Start over

**Supported formats:** PDF, DOCX

**Commands:**
/start - Begin
/analyze - Show all scores
/modify - Get improved resume
/reset - Clear session
/help - This message
"""
    await update.message.reply_text(help_text, parse_mode='Markdown')

def main():
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN environment variable not set")
    if not os.environ.get('OPENROUTER_API_KEY'):
        raise ValueError("OPENROUTER_API_KEY environment variable not set")
    
    app = Application.builder().token(token).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("analyze", show_analysis))
    app.add_handler(CommandHandler("modify", modify_resume))
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            WAITING_FOR_JD: [MessageHandler(filters.TEXT | filters.Document.ALL, handle_jd)],
            WAITING_FOR_RESUME: [
                MessageHandler(filters.Document.ALL, handle_resume),
                CommandHandler("done", lambda u, c: WAITING_FOR_ACTION),
                CommandHandler("analyze", show_analysis),
                CommandHandler("modify", modify_resume),
            ],
        },
        fallbacks=[CommandHandler("reset", reset)],
    )
    
    app.add_handler(conv_handler)
    
    app.run_polling()

if __name__ == '__main__':
    main()
