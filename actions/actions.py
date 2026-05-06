from typing import Any, Text, Dict, List
from rasa_sdk import Action, Tracker
from rasa_sdk.executor import CollectingDispatcher

class ActionSetRole(Action):
    def name(self) -> Text:
        return "action_set_role"

    def run(self, dispatcher: CollectingDispatcher,
            tracker: Tracker,
            domain: Dict[Text, Any]) -> List[Dict[Text, Any]]:
        # Просто подтверждаем установку роли
        role = tracker.get_slot("role")
        if role:
            dispatcher.utter_message(text=f"Отлично, фиксирую роль: {role}")
        return []

class ActionEvaluateCandidate(Action):
    def name(self) -> Text:
        return "action_evaluate_candidate"

    def run(self, dispatcher: CollectingDispatcher,
            tracker: Tracker,
            domain: Dict[Text, Any]) -> List[Dict[Text, Any]]:
        
        role = tracker.get_slot("role")
        exp = tracker.get_slot("experience_years")
        
        # Простая бизнес-логика (заглушка)
        # Если в строке опыта есть цифра > 2, считаем подходящим
        is_experienced = any(char.isdigit() and int(char) >= 2 for char in str(exp))
        
        if is_experienced:
            dispatcher.utter_message(text=f"✅ Вы отлично подходите на роль {role}! Мы свяжемся для тех-собеса.")
        else:
            dispatcher.utter_message(text=f"У вас интересный профиль, но на роль {role} мы ищем кого-то с чуть большим опытом. Мы сохраним ваше резюме.")
            
        return []
