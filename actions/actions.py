"""
HR-бот: кастомные actions для скрининга кандидатов.

Содержит:
- ActionSetRole: нормализует и сохраняет роль кандидата в slot.
- ActionEvaluateCandidate: оценивает кандидата по критериям роли.
- ActionSummary: формирует итоговое резюме с вердиктом.
- ActionRestart: сбрасывает все slots и перезапускает диалог.
"""

from typing import Any, Dict, List, Optional, Text, Tuple
import re
import logging

from rasa_sdk import Action, Tracker
from rasa_sdk.events import SlotSet, Restarted, AllSlotsReset
from rasa_sdk.executor import CollectingDispatcher

logger = logging.getLogger(__name__)


# ==============================================================================
# НОРМАЛИЗАЦИЯ РОЛЕЙ
# ==============================================================================
# Приводим любые написания роли к канонической форме (lowercase английский).
# Это нужно, потому что пользователь может писать "ДС", "data scientist",
# "дата саентист" — а в коде хотим один ключ для справочника требований.

ROLE_ALIASES: Dict[str, str] = {
    # Data Scientist
    "ds": "data scientist",
    "data science": "data scientist",
    "data scientist": "data scientist",
    "дс": "data scientist",
    "дата саентист": "data scientist",
    "дата саенс": "data scientist",

    # Data Engineer
    "de": "data engineer",
    "data engineer": "data engineer",
    "де": "data engineer",
    "дата инженер": "data engineer",
    "инженер данных": "data engineer",

    # Data Analyst
    "da": "data analyst",
    "data analyst": "data analyst",
    "да": "data analyst",
    "аналитик данных": "data analyst",
    "дата аналитик": "data analyst",

    # Project Manager
    "pm": "project manager",
    "project manager": "project manager",
    "product manager": "project manager",
    "product owner": "project manager",
    "po": "project manager",
    "проджект": "project manager",
    "проджект менеджер": "project manager",
    "продакт": "project manager",
    "продакт менеджер": "project manager",
    "продукт менеджер": "project manager",
    "руководитель проекта": "project manager",

    # MLOps Engineer
    "mlops": "mlops engineer",
    "mlops engineer": "mlops engineer",
    "mlops инженер": "mlops engineer",
    "devops": "mlops engineer",
    "devops engineer": "mlops engineer",
    "девопс": "mlops engineer",
    "sre": "mlops engineer",
}


def normalize_role(raw_role: Optional[str]) -> Optional[str]:
    """
    Приводит роль к канонической форме.
    
    Args:
        raw_role: сырая строка от пользователя ("DS", "дата саентист" и т.п.)
    
    Returns:
        Каноническое название ("data scientist") или None, если не распознано.
    """
    if not raw_role:
        return None
    key = str(raw_role).strip().lower()
    return ROLE_ALIASES.get(key)


# ==============================================================================
# ТРЕБОВАНИЯ ДЛЯ КАЖДОЙ РОЛИ
# ==============================================================================
# Для каждой роли задаём:
# - min_experience_years: минимальный опыт
# - required_skills: нужно совпадение ХОТЯ БЫ с одним навыком из этого множества
# - accepted_formats: допустимые форматы работы
# - max_salary: потолок зарплаты (руб)

ROLE_REQUIREMENTS: Dict[str, Dict[str, Any]] = {
    "data scientist": {
        "min_experience_years": 1.0,
        "required_skills": {
            "python", "машинное обучение", "ml",
            "pytorch", "tensorflow", "sklearn", "nlp", "cv",
        },
        "accepted_formats": {"удаленка", "гибрид", "офис", "гибкий"},
        "max_salary": 500000,
    },
    "data engineer": {
        "min_experience_years": 1.5,
        "required_skills": {
            "python", "sql", "etl", "airflow", "spark", "kafka",
        },
        "accepted_formats": {"удаленка", "гибрид", "офис", "гибкий"},
        "max_salary": 450000,
    },
    "data analyst": {
        "min_experience_years": 0.5,
        "required_skills": {
            "sql", "python", "tableau", "power bi",
            "excel", "анализ данных",
        },
        "accepted_formats": {"удаленка", "гибрид", "офис", "гибкий"},
        "max_salary": 550000,
    },
    "project manager": {
        "min_experience_years": 2.0,
        "required_skills": {
            "scrum", "agile", "jira",
            "управление проектами", "kanban",
        },
        "accepted_formats": {"гибрид", "офис"},
        "max_salary": 400000,
    },
    "mlops engineer": {
        "min_experience_years": 2.0,
        "required_skills": {
            "docker", "kubernetes", "mlflow",
            "ci/cd", "linux", "python",
        },
        "accepted_formats": {"удаленка", "гибрид", "офис", "гибкий"},
        "max_salary": 500000,
    },
}


# ==============================================================================
# ПАРСЕРЫ
# ==============================================================================

def parse_experience_years(raw: Optional[str]) -> Optional[float]:
    """
    Извлекает опыт работы в годах.
    
    Примеры:
        "3 года"       → 3.0
        "1.5 года"     → 1.5
        "1,5 года"     → 1.5
        "полгода"      → 0.5
        "6 месяцев"    → 0.5
        "18 месяцев"   → 1.5
        "много"        → None
    """
    if not raw:
        return None
    text = str(raw).strip().lower().replace(",", ".")
    
    # "полгода" / "пол года"
    if "полгода" in text or "пол года" in text or "пол-года" in text:
        return 0.5
    
    # Месяцы: "6 месяцев", "18 мес"
    m = re.search(r"(\d+(?:\.\d+)?)\s*мес", text)
    if m:
        return float(m.group(1)) / 12.0
    
    # Годы: "3 года", "5 лет"
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:год|лет|г\.)", text)
    if m:
        return float(m.group(1))
    
    # Просто число: "10" или "3+"
    m = re.search(r"^(\d+(?:\.\d+)?)\s*\+?$", text)
    if m:
        return float(m.group(1))
    
    logger.warning(f"Could not parse experience: {raw!r}")
    return None


def parse_salary(raw: Optional[str]) -> Optional[int]:
    """
    Извлекает зарплату в рублях.
    
    Примеры:
        "150000"        → 150000
        "250k" / "250к" → 250000
        "0.5 млн"       → 500000
        "1 млн"         → 1_000_000
        "240-280 тыс"   → 260000 (среднее диапазона)
        "договорная"    → None
    """
    if not raw:
        return None
    text = str(raw).strip().lower().replace(",", ".").replace(" ", "")
    
    # Диапазон: "240-280к"
    m = re.search(
        r"(\d+(?:\.\d+)?)[-–—](\d+(?:\.\d+)?)(к|k|тыс|тысяч)?",
        text,
    )
    if m:
        low = float(m.group(1))
        high = float(m.group(2))
        mult = 1000 if m.group(3) else 1
        return int((low + high) / 2 * mult)
    
    # Миллионы: "0.5 млн"
    m = re.search(r"(\d+(?:\.\d+)?)млн", text)
    if m:
        return int(float(m.group(1)) * 1_000_000)
    
    # Тысячи: "250k", "300тыс"
    m = re.search(r"(\d+(?:\.\d+)?)(?:k|к|тыс|тысяч)", text)
    if m:
        return int(float(m.group(1)) * 1000)
    
    # Просто число: "150000"
    m = re.search(r"(\d+)", text)
    if m:
        return int(m.group(1))
    
    logger.warning(f"Could not parse salary: {raw!r}")
    return None


def _is_plain_number(text: str) -> Optional[float]:
    """Возвращает число для сообщений вида '5' / '3.5' / '3+'."""
    if not text:
        return None
    normalized = text.strip().lower().replace(",", ".")
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\+?", normalized)
    if not m:
        return None
    return float(m.group(1))


def _has_experience_markers(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    return bool(
        re.search(
            r"(опыт|стаж|год|года|лет|г\.|мес|месяц|месяца|месяцев|полгода|пол года)",
            t,
        )
    )


def _has_salary_markers(text: str) -> bool:
    if not text:
        return False
    t = text.lower()
    return bool(
        re.search(
            r"(зарп|зп|оклад|доход|руб|₽|тыс|тысяч|млн|k|к|gross|net|на руки|чист|гряз|до вычета|после налогов)",
            t,
        )
    )


SKILL_SYNONYMS: Dict[str, str] = {
    # Python
    "python": "python",
    "py": "python",
    "питон": "python",
    "пайтон": "python",
    "питн": "python",
    "пайтн": "python",
    "phyton": "python",
    "python3": "python",
    "python 3": "python",
    "питон3": "python",

    # SQL
    "sql": "sql",
    "postgresql": "sql",
    "postgres": "sql",
    "postgre": "sql",
    "mysql": "sql",
    "mssql": "sql",
    "ms sql": "sql",
    "oracle sql": "sql",
    "sqlite": "sql",
    "sql server": "sql",
    "structured query language": "sql",
    "скьюэль": "sql",
    "эс кью эль": "sql",
    "скл": "sql",
    "сиквел": "sql",

    # Machine learning
    "ml": "машинное обучение",
    "machine learning": "машинное обучение",
    "машинное обучение": "машинное обучение",
    "маш обучение": "машинное обучение",
    "машин лернинг": "машинное обучение",
    "машин ленинг": "машинное обучение",
    "машленинг": "машинное обучение",
    "мл": "машинное обучение",
    "ai/ml": "машинное обучение",
    "predictive modeling": "машинное обучение",

    # Deep learning and frameworks
    "deep learning": "машинное обучение",
    "нейросети": "машинное обучение",
    "нейронки": "машинное обучение",
    "нейронные сети": "машинное обучение",
    "tensorflow": "tensorflow",
    "tf": "tensorflow",
    "тензорфлоу": "tensorflow",
    "тенсорфлоу": "tensorflow",
    "tensor flow": "tensorflow",
    "pytorch": "pytorch",
    "torch": "pytorch",
    "пайторч": "pytorch",
    "питорч": "pytorch",
    "scikit-learn": "sklearn",
    "scikit learn": "sklearn",
    "sklearn": "sklearn",
    "sci-kit learn": "sklearn",
    "сайкит": "sklearn",
    "скикит": "sklearn",

    # DS areas
    "nlp": "nlp",
    "natural language processing": "nlp",
    "обработка естественного языка": "nlp",
    "обработка текста": "nlp",
    "text mining": "nlp",
    "cv": "cv",
    "computer vision": "cv",
    "компьютерное зрение": "cv",
    "комп зрение": "cv",
    "vision": "cv",

    # Data engineering
    "etl": "etl",
    "elt": "etl",
    "пайплайны данных": "etl",
    "data pipelines": "etl",
    "интеграция данных": "etl",
    "airflow": "airflow",
    "apache airflow": "airflow",
    "эйрфлоу": "airflow",
    "аирфлоу": "airflow",
    "spark": "spark",
    "apache spark": "spark",
    "pyspark": "spark",
    "спарк": "spark",
    "kafka": "kafka",
    "apache kafka": "kafka",
    "кафка": "kafka",
    "kafkа": "kafka",

    # BI and analytics
    "tableau": "tableau",
    "табло": "tableau",
    "таблеу": "tableau",
    "power bi": "power bi",
    "powerbi": "power bi",
    "power-bi": "power bi",
    "пауэр би": "power bi",
    "павер би": "power bi",
    "excel": "excel",
    "ms excel": "excel",
    "microsoft excel": "excel",
    "эксель": "excel",
    "ексель": "excel",
    "анализ данных": "анализ данных",
    "data analysis": "анализ данных",
    "data analytics": "анализ данных",
    "продуктовая аналитика": "анализ данных",
    "a/b тесты": "анализ данных",

    # PM
    "scrum": "scrum",
    "скрам": "scrum",
    "scram": "scrum",
    "agile": "agile",
    "аджайл": "agile",
    "agille": "agile",
    "jira": "jira",
    "джира": "jira",
    "jira software": "jira",
    "управление проектами": "управление проектами",
    "project management": "управление проектами",
    "pm practices": "управление проектами",
    "kanban": "kanban",
    "канбан": "kanban",
    "kanban board": "kanban",

    # MLOps and infra
    "docker": "docker",
    "докер": "docker",
    "доккер": "docker",
    "docker-compose": "docker",
    "docker compose": "docker",
    "kubernetes": "kubernetes",
    "k8s": "kubernetes",
    "kubectl": "kubernetes",
    "кубер": "kubernetes",
    "куб": "kubernetes",
    "кубернетес": "kubernetes",
    "кубернетс": "kubernetes",
    "kubernets": "kubernetes",
    "mlflow": "mlflow",
    "ml flow": "mlflow",
    "эмэльфлоу": "mlflow",
    "ci/cd": "ci/cd",
    "ci cd": "ci/cd",
    "cicd": "ci/cd",
    "github actions": "ci/cd",
    "гитхаб экшнс": "ci/cd",
    "gitlab ci": "ci/cd",
    "jenkins": "ci/cd",
    "linux": "linux",
    "gnu/linux": "linux",
    "linuks": "linux",
    "линукс": "linux",
    "ubuntu": "linux",
}


def normalize_skills(raw: Any) -> set:
    """
    Приводит навыки к множеству lowercase-строк.
    Поддерживает:
    - строку: "SQL, Python" → разбить и нормализовать
    - список строк: ["SQL", "Python"]
    - список из одной строки: ["SQL, Python"] → разбить
    """
    if not raw:
        return set()

    items = []

    if isinstance(raw, str):
        # Разбиваем строку
        items = re.split(r"[,;/\n]", raw)
    elif isinstance(raw, (list, tuple, set)):
        # Если список — проходим по каждому элементу
        for item in raw:
            if isinstance(item, str):
                # Если элемент — строка с запятыми, разбиваем
                parts = re.split(r"[,;/\n]", str(item))
                items.extend(parts)
            else:
                items.append(str(item))
    else:
        return set()

    result = set()
    for item in items:
        key = str(item).strip().lower()
        if key:
            # Применяем синонимы
            normalized = SKILL_SYNONYMS.get(key, key)
            result.add(normalized)
    return result


def normalize_format(raw: Optional[str]) -> Optional[str]:
    """Приводит формат работы к канонической форме."""
    if not raw:
        return None
    text = str(raw).strip().lower()
    aliases = {
        "remote": "удаленка",
        "full remote": "удаленка",
        "удаленно": "удаленка",
        "удалёнка": "удаленка",
        "удаленная": "удаленка",
        "hybrid": "гибрид",
        "гибридный": "гибрид",
        "офис": "офис",
        "гибрид": "гибрид",
        "гибкий": "гибкий",
        "office": "офис",
        "офисный": "офис",
        "в офисе": "офис",
        "flexible": "гибкий",
        "flex": "гибкий",
        "part-time": "гибкий",
        "неполный день": "гибкий",
        "без разницы": "гибрид",
        "мне без разницы": "гибрид",
        "любой": "гибрид",
        "не важно": "гибрид",
        "не имеет значения": "гибрид",
        "любой формат подойдет": "гибрид",
    }
    return aliases.get(text, text)


# ==============================================================================
# ЯДРО ОЦЕНКИ КАНДИДАТА
# ==============================================================================

def evaluate_candidate(
    role: Optional[str],
    experience_years: Optional[str],
    skills: Any,
    salary_expectation: Optional[str],
    work_format: Optional[str],
) -> Tuple[bool, str]:
    """
    Оценивает кандидата по критериям роли.
    
    Args:
        role: название роли (любое написание)
        experience_years: опыт работы (строка)
        skills: список или строка навыков
        salary_expectation: зарплатные ожидания (строка)
        work_format: формат работы (строка)
    
    Returns:
        (is_fit, explanation):
            is_fit: True если подходит, False если нет
            explanation: человекочитаемое объяснение
    """
    # 1. Проверка роли
    if not role:
        return False, "Роль не указана."
    
    canonical_role = normalize_role(role) or str(role).lower()
    requirements = ROLE_REQUIREMENTS.get(canonical_role)
    
    if not requirements:
        return False, (
            f"К сожалению, роль '{role}' не входит в список открытых позиций."
        )
    
    # 2. Парсим данные кандидата
    candidate_exp = parse_experience_years(experience_years)
    candidate_skills = normalize_skills(skills)
    candidate_salary = parse_salary(salary_expectation)
    candidate_format = normalize_format(work_format)
    
    reasons: List[str] = []
    
    # 3. Проверка опыта
    if candidate_exp is None:
        reasons.append("не удалось определить опыт работы")
    elif candidate_exp < requirements["min_experience_years"]:
        reasons.append(
            f"опыт {candidate_exp} лет меньше требуемого "
            f"{requirements['min_experience_years']} лет"
        )
    
    # 4. Проверка навыков
    required = requirements["required_skills"]
    matched = candidate_skills & required
    if not matched:
        reasons.append(
            f"не указан ни один из ключевых навыков "
            f"({', '.join(sorted(required))})"
        )
    
    # 5. Проверка формата работы
    if candidate_format and candidate_format not in requirements["accepted_formats"]:
        reasons.append(
            f"формат '{candidate_format}' не подходит "
            f"(допустимы: {', '.join(sorted(requirements['accepted_formats']))})"
        )
    
    # 6. Проверка зарплаты
    if candidate_salary and candidate_salary > requirements["max_salary"]:
        reasons.append(
            f"зарплатные ожидания ({candidate_salary:,} руб) выше потолка "
            f"({requirements['max_salary']:,} руб)"
        )
    
    # 7. Итог
    if not reasons:
        return True, (
            f"Вы подходите на роль {canonical_role.title()}! "
            f"Совпадают навыки: {', '.join(sorted(matched))}."
        )
    else:
        return False, (
            f"К сожалению, на роль {canonical_role.title()} не проходите: "
            + "; ".join(reasons) + "."
        )


# ==============================================================================
# ACTION: SET ROLE
# ==============================================================================

class ActionSetRole(Action):
    """
    Нормализует и сохраняет роль кандидата в slot.
    
    Логика:
    1. Читаем текущий slot 'role'.
    2. Если пустой — берём entity из последнего сообщения.
    3. Нормализуем через ROLE_ALIASES.
    4. Сохраняем каноническое значение или просим уточнить.
    """

    def name(self) -> Text:
        return "action_set_role"
            
    def run( self,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any],
    ) -> List[Dict[Text, Any]]:
        # 1. Берём текущее значение slot
        current_role = tracker.get_slot("role")
        
        # 2. Если пусто — пытаемся достать из entity
        if not current_role:
            current_role = next(
                tracker.get_latest_entity_values("role"), None
            )
        
        # 3. Если всё ещё пусто — ищем в тексте сообщения напрямую
        if not current_role:
            last_message = tracker.latest_message.get("text", "").strip().lower()
            current_role = last_message  # пробуем нормализовать весь текст
        
        # 4. Нормализуем
        canonical_role = normalize_role(current_role)
        
        if canonical_role:
            logger.info(f"Role set to: {canonical_role}")
            dispatcher.utter_message(
                text=f"Отлично, фиксирую роль: {canonical_role.title()}"
            )
            return [SlotSet("role", canonical_role)]
        else:
            logger.warning(f"Could not normalize role: {current_role!r}")
            dispatcher.utter_message(
                text=(
                    "Не распознал роль. Доступные позиции: "
                    "Data Scientist, Data Engineer, Data Analyst, "
                    "Project Manager, MLOps Engineer. Напишите название роли."
                )
            )
            return [SlotSet("role", None)]


# ==============================================================================
# ACTION: PROCESS EXPERIENCE INPUT
# ==============================================================================

class ActionProcessExperience(Action):
    """
    Обрабатывает ввод опыта с учетом неоднозначных чисел:
    - если пришло "5" на шаге опыта -> считаем опытом;
    - если пришло "250000"/"250к" -> считаем, что это ЗП и просим уточнить опыт.
    """

    def name(self) -> Text:
        return "action_process_experience"

    def run(
        self,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any],
    ) -> List[Dict[Text, Any]]:
        text = (tracker.latest_message.get("text") or "").strip()
        parsed_exp = parse_experience_years(text)
        parsed_salary = parse_salary(text)
        plain_number = _is_plain_number(text)
        has_exp_markers = _has_experience_markers(text)
        has_salary_markers = _has_salary_markers(text)

        # Явно зарплатный ввод на этапе опыта.
        if has_salary_markers and parsed_salary is not None:
            dispatcher.utter_message(
                text=(
                    "Похоже, это зарплатные ожидания. "
                    "Сейчас фиксируем опыт: напишите, пожалуйста, в формате "
                    "'3 года', '1.5 года' или '6 месяцев'."
                )
            )
            return [
                SlotSet("experience_years", None),
                SlotSet("salary_expectation", None),
            ]

        # Большое голое число без маркеров — почти точно зарплата.
        if (
            plain_number is not None
            and plain_number > 30
            and not has_exp_markers
            and not has_salary_markers
        ):
            dispatcher.utter_message(
                text=(
                    "Число похоже на зарплату, а сейчас нужен опыт. "
                    "Напишите опыт, например: '2 года' или '18 месяцев'."
                )
            )
            return [
                SlotSet("experience_years", None),
                SlotSet("salary_expectation", None),
            ]

        # Нормальный случай: воспринимаем как опыт.
        if parsed_exp is not None:
            return [
                SlotSet("experience_years", text),
                # Если интент распознали как salary на шаге опыта — не держим мусор.
                SlotSet("salary_expectation", None),
            ]

        dispatcher.utter_message(
            text=(
                "Не удалось распознать опыт. Напишите, пожалуйста, "
                "в формате '3 года', '1.5 года' или '6 месяцев'."
            )
        )
        return [SlotSet("experience_years", None)]


# ==============================================================================
# ACTION: PROCESS SALARY INPUT
# ==============================================================================

class ActionProcessSalary(Action):
    """
    Обрабатывает ввод зарплаты с учетом неоднозначных чисел:
    - если на шаге ЗП пришло '5' -> вероятно опыт, просим уточнить ЗП;
    - если пришло '250к' / '300000' -> фиксируем как зарплату.
    """

    def name(self) -> Text:
        return "action_process_salary"

    def run(
        self,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any],
    ) -> List[Dict[Text, Any]]:
        text = (tracker.latest_message.get("text") or "").strip()
        parsed_salary = parse_salary(text)
        parsed_exp = parse_experience_years(text)
        plain_number = _is_plain_number(text)
        has_exp_markers = _has_experience_markers(text)
        has_salary_markers = _has_salary_markers(text)

        # Явно опытный ввод на шаге зарплаты.
        if has_exp_markers and not has_salary_markers:
            dispatcher.utter_message(
                text=(
                    "Понял про опыт. Сейчас собираем зарплатные ожидания: "
                    "напишите, пожалуйста, например '250к', '300000' "
                    "или '240-280к'."
                )
            )
            return [SlotSet("salary_expectation", None)]

        # Маленькое голое число без валютных маркеров чаще означает опыт.
        if (
            plain_number is not None
            and plain_number <= 30
            and not has_salary_markers
            and parsed_exp is not None
        ):
            dispatcher.utter_message(
                text=(
                    "Похоже, это количество лет опыта, а здесь нужна зарплата. "
                    "Укажите ожидания, например: '250к' или '300000 на руки'."
                )
            )
            return [SlotSet("salary_expectation", None)]

        if parsed_salary is not None:
            return [
                SlotSet("salary_expectation", text),
                # Если интент ошибочно был про опыт на шаге ЗП — не затираем опыт мусором.
                SlotSet("experience_years", tracker.get_slot("experience_years")),
            ]

        dispatcher.utter_message(
            text=(
                "Не удалось распознать зарплату. Напишите, пожалуйста, "
                "в формате '250к', '300000' или '240-280к'."
            )
        )
        return [SlotSet("salary_expectation", None)]


# ==============================================================================
# ACTION: EVALUATE CANDIDATE
# ==============================================================================

class ActionEvaluateCandidate(Action):
    """
    Оценивает кандидата на основе slots и записывает результат
    в slot 'evaluation_result'.
    """

    def name(self) -> Text:
        return "action_evaluate_candidate"

    def run(
        self,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any],
    ) -> List[Dict[Text, Any]]:
        role = tracker.get_slot("role")
        experience = tracker.get_slot("experience_years")
        skills = tracker.get_slot("skills")
        salary = tracker.get_slot("salary_expectation")
        work_format = tracker.get_slot("work_format")
        
        is_fit, explanation = evaluate_candidate(
            role, experience, skills, salary, work_format
        )
        
        verdict = f"{'✅ ПОДХОДИТ' if is_fit else '❌ НЕ ПОДХОДИТ'}: {explanation}"
        logger.info(f"Evaluation: {verdict}")
        
        return [SlotSet("evaluation_result", verdict)]


# ==============================================================================
# ACTION: SUMMARY
# ==============================================================================
class ActionSummary(Action):
    def name(self) -> Text:
        return "action_summary"

    def run(
        self,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any],
    ) -> List[Dict[Text, Any]]:
        # Читаем slots c fallback на "не указана"
        role = tracker.get_slot("role") or "не указана"
        experience = tracker.get_slot("experience_years") or "не указан"
        skills = tracker.get_slot("skills") or []
        salary = tracker.get_slot("salary_expectation") or "не указана"
        work_format = tracker.get_slot("work_format") or "не указан"

        skills_str = (
            ", ".join(skills) if isinstance(skills, list) else str(skills)
        )

        # Считываем результат оценки из слота
        # evaluation_result содержит строку с эмодзи и пояснением, например "✅ Подходит: Совпадают навыки: Python."
        verdict_message = tracker.get_slot("evaluation_result")

        # Парсим результат оценки
        verdict_message = tracker.get_slot("evaluation_result")

        if not verdict_message or not isinstance(verdict_message, str):
            explanation = "Не удалось оценить кандидата: данные отсутствуют."
        else:
            if verdict_message.startswith("✅") or verdict_message.startswith("❌"):
                verdict_emoji = verdict_message[0]
                explanation = verdict_message[1:].strip()
            else:
                verdict_emoji = "ℹ️"
                explanation = verdict_message.strip()

        summary = (
            f"--> Резюме вашей анкеты:\n"
            f"---------------------\n"
            f"Роль: {role.title()}\n" # role.title() для красивого вывода
            f"Опыт: {experience}\n"
            f"Навыки: {skills_str}\n"
            f"ЗП: {salary}\n"
            f"Формат: {work_format.title()}\n" # work_format.title() для красивого вывода
            f"---------------------\n"
            f"{verdict_emoji} {explanation}"
        )
        dispatcher.utter_message(text=summary)
        return [SlotSet("evaluation_result", explanation)] # Можно установить только пояснение, без эмодзи, в слот, если это нужно дальше.

# ==============================================================================
# ACTION: RESTART
# ==============================================================================

class ActionRestart(Action):
    """
    Сбрасывает все slots и перезапускает трекер.
    Используется, когда пользователь говорит "начать заново".
    """

    def name(self) -> Text:
        return "action_restart"

    def run(
        self,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any],
    ) -> List[Dict[Text, Any]]:
        dispatcher.utter_message(text="Диалог сброшен. Начнём сначала! 🔄")
        return [AllSlotsReset(), Restarted()]
    

class ActionAskSkills(Action):
    """Задаёт вопрос про навыки в зависимости от роли."""

    def name(self) -> Text:
        return "action_ask_skills"

    def run(
        self,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any],
    ) -> List[Dict[Text, Any]]:
        role = tracker.get_slot("role")

        questions = {
            "data scientist": "Ваш ML-стек? (PyTorch, Sklearn, NLP/CV)",
            "data engineer": "Какие инструменты данных используете? (Airflow, Spark, Kafka)",
            "data analyst": "Ваш стек в аналитике? (SQL, Tableau, Python)",
            "project manager": "Какие методологии управления знаете? (Scrum, Kanban, Jira)",
            "mlops engineer": "Инструменты MLOps? (Docker, K8s, MLflow)",
        }

        question = questions.get(
            role, "Какие у вас ключевые навыки?"
        )
        dispatcher.utter_message(text=question)
        return []
