import numpy as np
import pandas as pd
import subprocess
import collections

import sys
import re
from paramiko import SSHClient, AutoAddPolicy
from io import StringIO

#Это просто мой способ коннекта к LLM, тут может быть какой-то ваш коннектор
sys.path.append('../llms_local')
import chat_bot


from selenium.webdriver import Chrome, ChromeOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoSuchElementException
import urllib.parse
import time
from urllib.parse import unquote


def setup_driver():
    chrome_options = ChromeOptions()
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("--start-maximized")
    return Chrome(options=chrome_options)

def parse_organic_item(item):
    """Парсим элемент с объединенным заголовком и сниппетом"""
    try:
        content = item.find_element(By.CSS_SELECTOR, ".OrganicTextContentSpan")
        full_text = content.text.replace('\n', ' ')
        
        # Разделяем текст на заголовок и сниппет
        parts = full_text.split('. ')
        title = parts[0] + '.' if len(parts) > 1 else full_text
        snippet = '. '.join(parts[1:]) if len(parts) > 1 else ""
        
        url = item.find_element(By.CSS_SELECTOR, "a[href]").get_attribute("href")
        
        return {
            'title': title,
            'url': url.split('?')[0],
            'snippet': snippet[:250] + '...' if len(snippet) > 250 else snippet
        }
        
    except NoSuchElementException:
        return None

def smart_parse(driver):
    results = []
    try:
        items = WebDriverWait(driver, 15).until(
            EC.presence_of_all_elements_located((By.CSS_SELECTOR, "li.serp-item")))
        
        for item in items[:30]:
            result = parse_organic_item(item)
            if result and 'yandex.ru' not in result['url']:
                results.append(result)
                
        return results
    
    except Exception as e:
        print(f"Parse error: {str(e)}")
        return []

def ya_search(query):
    driver = setup_driver()
    try:
        url = f"https://yandex.ru/search/?text={urllib.parse.quote_plus(query)}"
        driver.get(url)
        time.sleep(2)  # Ожидание прогрузки JS
        
        # Пролистываем страницу
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight/2)")
        time.sleep(1)
        
        return str(smart_parse(driver))
        
    finally:
        driver.quit()


#Это просто мой способ коннекта к LLM, тут может быть какой-то ваш коннектор
user_id = int(np.random.rand()*1e8)
llm_connector = chat_bot.ChatBot(user_id)
llm_connector.name_in_promt = "2-342043824308240834208"


def english_character_fraction(s):
    total_chars = len(s)
    if total_chars == 0:
        return 0.0  # Избегаем деления на ноль

    english_chars = sum(1 for char in s if char.isalpha() and char.isascii())
    fraction = english_chars / total_chars
    return fraction




class SSHTool:
    def __init__(self, vm_ip, user='sd', out_max_size=700, passwd_file="../llms_local/ssh_pss.txt"):
        self.client = SSHClient()
        self.client.set_missing_host_key_policy(AutoAddPolicy())
        #self.client.connect(vm_ip, username=user, key_filename=key_path)
        with open(passwd_file,'r') as f:
            password = f.read()
        self.client.connect(vm_ip, username=user, password=password )
        self.out_max_size = out_max_size

        self.exec_timeout = 30
    
    def execute(self, command: str, verbose=False) -> str:
        # Фильтрация опасных команд (например, rm, chmod, sudo)
        #if any(blocked in command for blocked in ["rm", "chmod", "sudo"]):
        #    return "Error: Command blocked by security policy"

        try:
            stdin, stdout, stderr = self.client.exec_command(command, timeout=self.exec_timeout)
            result = StringIO()
            result.write(stdout.read().decode())
            errlog = stderr.read().decode()
            if len(errlog) > 2:
                result.write("\nERROR:\n" + errlog)
            if verbose:
                print('SSH in:', command)
                print('SSH out', result.getvalue()[:self.out_max_size])
            return result.getvalue()[:self.out_max_size]
        except Exception:
            if verbose:
                print('SSH in:', command)
                print('TIMEOUT')
            
            return f"Команда, которую вы послали, исполняется дольше {self.exec_timeout} секунд, и потому она была отключена. Нельзя делать такие длинные запросы. Нельзя запускать программы в интерактивном режиме (например, интерпретатор python3 или bash без файла скрипта). Если очень нужно сделать долго работающий код, запускай его в фоновом режиме. В большинстве случае достаточно просто запускать/открывать файл в не-интерактивном режиме, например `bash test.sh` или `python test.py` или `cat test.txt`"


class ToolBot:
    def __init__(self, character_prompt, character_name, instruction_prompt=None, user_name="sd", termimal_mode = 'bash', safety_check_bash=True, sshInstance=None, max_tokens_in_msg=270, history_text_maxlen=4500, history_maxlen=200):
        self.llm_connector = llm_connector
        self.character_prompt = character_prompt
        self.character_name = character_name
        self.instruction_prompt = instruction_prompt
        self.user_name = user_name
        self.history = collections.deque(maxlen=history_maxlen)#[]
        self.current_mode = 'user'  # Начинаем в режиме пользователя
        self.safety_check_bash = safety_check_bash
        self.termimal_mode = termimal_mode

        self.max_tokens_in_msg =  max_tokens_in_msg
        self.history_text_maxlen = history_text_maxlen
        self.sshInstance = sshInstance
        self.log = []

        if instruction_prompt is None:
            self.instruction_prompt = (
    "Тебе доступны следующие режимы: \n1) диалог с собеседником \n2) заметки для размышлений.\n3) поисковик. \n4) работа с терминалом \nСмело пользуйся терминалом. Если тебя просят что-то сделать или узнать - не надо рассказывать инструкцию, перейди в режим терминала и сделай! Обращай внимание, в каком ты сейчас режиме!"
    "Тебе явным образом предложат выбрать режим перед каждым сообщением - не пытайся менять режим в другие моменты.\n"
    "В терминальном режиме ты подключён к компьютеру собеседника, и можешь запускать там различные команды. Не стесняйся - применяй, когда захочешь! Доступ к терминалу позволяет, помимо прочего, использовать интернет и язык программирования. Если тебе надо искать информацию - ищи здесь. Для работы с файлами рекомендуется использовать cat и echo (не забывай проверять свой результат работы). Для работы с интеретом - curl + grep, lynx. Если для дальнейших действия надо что-то знать об операционке или о компе - ты это можешь выяснить в терминале. Если не знаешь чего-то про комп - или пробей через терминал, или спроси. Если у тебя не получатся что-либо распарсить, имеет смысл посмотреть на сырые данные. Кроме того, если ты знаешь ключевое слово, которое ищешь, то имеет смысл найти его через find в питоне, и вывести кусок текста в некотором диапазоне вокруг результата find. Ответы от терминала будут выглядеть как адресованные от юзера TERMINAL.\n"
    "Режим заметок нужен для обдумывания задачи - декомпозиции, проверки гипотез и так далее. Запускай, когда надо обдумать, прежде чем действовать\n"
    "Режим поисковика (search) - это доступ к поисковой строке яндекса. Через curl нельзя использовать основные поисковики (google, yandex, duckduckgo), они считают тебя ботом и блочат. А вот через режим поиска - можно! Это законное апи. Когда ты делаешь запрос к поисковой строке, ты получишь несколько пар заголовок-ссылка. Сами ссылки можешь потом выгрузить через curl и найти в них то, что тебе надо.\n"
    "Ответы, подписанные как TERMINAL - это выдача командной строки\n"
    "Если ты пишешь подстроку '<stop>', тут же отправляешь сообщение. Если хочешь закончить/отправить сообщение, пиши '<stop>'\n"
    "Ты пишешь ТОЛЬКО свою реплику, в конце <stop>\n"
    "Если ты перешёл в терминал, можешь проворачивать там всякие сложные многокомпонентные планы. Необязательно действовать в лоб\n"
    "Когда дело сделано, вернись в диалог с пользователем через '<user>'\n"
)
        else:
            self.instruction_prompt = instruction_prompt

    def switch_mode(self, mode):
        if mode in ['<user>', '<terminal>', '<note>',  '<thought>','<search>', '<choose>']:
            self.current_mode = mode.replace('<', '').replace('>', '')
            #self.history.append(f"{self.character_name}: {mode}")
        else:
            raise ValueError(f"Режим должен быть '<user>' или '<terminal>' или '<note>' или '<thought>'или '<search>' или '<choose>', a он {mode}")

    def execute_terminal_command(self, command):
        command = command.replace("```", "")
        command = command.replace("bash\n", "")
        
        if self.safety_check_bash:
            acceptance = input(f'Я хочу выполнить команду {command}. Введите для подтверждения')
        else:
            acceptance = True
        if self.termimal_mode == 'bash':
            result = subprocess.run(command, shell=True, capture_output=True, text=True)
            s_out = result.stdout.strip() + result.stderr.strip()
        elif self.termimal_mode == 'ssh':
            s_out = self.sshInstance.execute(command, verbose=self.verbose)
            
        if len(s_out) == 0:
            s_out = '(терминал ничего не выдал)'
        return s_out

    def update_history(self, sender, message):
        message = message.strip() + '\n'
        whereto = ''
        if self.current_mode == 'terminal':
            whereto = " (to terminal)"
        elif self.current_mode == 'note':
            whereto = " (to notes)"
        elif self.current_mode == 'search':
            whereto = " (to search)"
        elif self.current_mode == 'user':
            whereto = " (to dialogue)"
        elif self.current_mode == 'thoughts':
            whereto = " (in thoughts)"
        else:
            whereto = f" (in mode: {self.current_mode})"
        if sender != self.character_name:
            whereto = ''
        self.history.append(f"{sender}{whereto}: {message}")

    def respond(self, message, max_actions=None, verbose=True):
        self.verbose = verbose
        if message[-1] != '\n':
            message += '\n'
        self.update_history(self.user_name, message)

        self.action_counter = 0
        while 1:
            if max_actions is not None:
                if self.action_counter >= max_actions:
                    llm_answer = "Дай мне ещё время поработать над этой задачей."
                    self.update_history(self.character_name, llm_answer)
                    break
            self.action_counter += 1
            #вначале мысли, потом всё остальное
            # запрашиваем у LLM мысли
            self.switch_mode("<thought>")
            # запрашиваем у LLM
            prompt = self.generate_prompt()
            stop_words = ['<stop>']
            llm_answer = self.llm_connector.create_text(prompt, max_tokens=self.max_tokens_in_msg, temp=0.9, stop_words=stop_words, verbose=False)
            self.log.append([prompt,llm_answer])
            llm_answer = llm_answer.replace('<stop>', '')
            self.update_history(self.character_name, llm_answer)
            #закончили мысли
            # выбираем действие
            self.switch_mode(f"<choose>")
            prompt = self.generate_prompt()
            stop_words = ['<stop>', '<terminal>', '<user>','<search>', '<note>']

            self.switch_mode('<note>')
            for i in range(10):
                temp = 0.6
                llm_answer = self.llm_connector.create_text(prompt, max_tokens=7, stop_words=stop_words, verbose=False)
                llm_answer = llm_answer.replace('<stop>', '')
                succes_choose = False
                llm_answer = llm_answer.replace('dialogue', 'user')
                for mode in ['<terminal>', '<user>', '<note>', '<search>']:
                    if mode.replace('<', '').replace('>', '') in llm_answer:
                        self.switch_mode(mode)
                        succes_choose = True
                        break
                temp *= 1.3
                if succes_choose:
                    break
            self.log.append([prompt,llm_answer])
            if self.current_mode == "note":
                #режим, в котором мы просто что-то пишем в ленту. По факту ещё один thoughts
                prompt = self.generate_prompt()
                llm_answer = self.llm_connector.create_text(prompt, max_tokens=self.max_tokens_in_msg, stop_words=stop_words, verbose=False)
                self.log.append([prompt,llm_answer])
                llm_answer = llm_answer.replace('<stop>', '')
                self.update_history(self.character_name, llm_answer)
                
            elif self.current_mode == "terminal":
                #теперь хреначим в terminal
                prompt = self.generate_prompt()
                llm_answer = self.llm_connector.create_text(prompt, max_tokens=self.max_tokens_in_msg, stop_words=stop_words, verbose=False)
                self.log.append([prompt,llm_answer])
                llm_answer = llm_answer.replace('<stop>', '')
                self.update_history(self.character_name, llm_answer)
                #ходит terminal
                result = self.execute_terminal_command(llm_answer)
                self.update_history("TERMINAL", result)

            elif self.current_mode == "search":
                #теперь хреначим в search
                prompt = self.generate_prompt()
                llm_answer = self.llm_connector.create_text(prompt, max_tokens=self.max_tokens_in_msg, stop_words=stop_words, verbose=False)
                self.log.append([prompt,llm_answer])
                llm_answer = llm_answer.replace('<stop>', '')
                llm_answer = llm_answer.split("\n")[0]
                self.update_history(self.character_name, llm_answer)
                #ходит search
                result = ya_search(llm_answer)
                self.update_history("SEARCH", result)
                
            elif self.current_mode == "user":
                #просто ещё реплика
                # запрашиваем у LLM
                prompt = self.generate_prompt()
                llm_answer = self.llm_connector.create_text(prompt, max_tokens=self.max_tokens_in_msg, stop_words=stop_words, verbose=False)
                self.log.append([prompt,llm_answer])
                llm_answer = llm_answer.replace('<stop>', '').replace('<user>', '`user`').replace('<terminal>', '`terminal`').replace('<note>', '`note`').replace('<search>', '`search`')
                self.update_history(self.character_name, llm_answer)
                break
            else:
                print('unknown mode llm_answer=', llm_answer, 'self.current_mode=', self.current_mode)
                1/0
                
        return llm_answer

    def generate_prompt(self):
        history_text = "<stop>".join(self.history)
        if len(history_text) > self.history_text_maxlen:
            history_text = history_text[-self.history_text_maxlen:]
        where_to_write = ''
        where_to_write_end = ''
        if self.current_mode == "terminal":
            where_to_write = f'Сейчас включён доступ к терминалу. Ты подключён к компьютеру собеседника, и можешь запускать там различные команды. Можешь даже создавать файлы с кодом и запускать их, если хочешь. То есть в этом режиме ты можешь писать ТОЛЬКО команды в терминал.\nУчитывай, что это команды именно в терминал, а не в среду программирования или гугл напрямую.\nИзбегай интерактивных команд (например, python или bash без конкретного скрипта) - они повиснут. То есть писать `python3 printf("x")` нельзя, а `python3 my_printer.py` можно. Вот это ``` тоже не используй.\nУчти, ты можешь за один раз написать порядка {int(self.max_tokens_in_msg/3)} слов, не больше.'
            where_to_write_end = ' (writing into terminal)'
        elif self.current_mode == "user":
            where_to_write_end = ' (to companon)'
            where_to_write = 'Сейчас включён доступ к собеседнику, ты с ним можешь общаться.\n'
        elif self.current_mode == "note":
            where_to_write_end = ' (writing to notes)'
            where_to_write = 'Сейчас включён доступ к заметкам, ты сюда можешь выписывать свои рассуждения, планы, декомпозицию задачи\n'
        elif self.current_mode == "search":
            where_to_write_end = ' (writing to search string)'
            where_to_write = 'Сейчас включён доступ к поисковику. Напиши в него короткий запрос и поставь символ <stop>\n'
        elif self.current_mode == "thought":
            where_to_write_end = ' (in your thoughts)'
            where_to_write = 'Сейчас ты думаешь. Твои мысли никто не видит. Команды в этом режиме не выполняются, режимы не переключаются. Ты можешь:\n * декомпозировать задачу\n * отмечать, выполнена ли задача, и если нет, то почему\n * проговаривать, что собираешься делать\n * анализировать обстановку\n'
        elif self.current_mode == "choose":
            where_to_write_end = ' (chosing mode)'
            where_to_write = 'Сейчас ты выбираешь режим - у тебя есть следующие варианты:\n<terminal> - перейти к командной строке\n<user> - перейти к диалогу\n<note> - перейти к записной книжке\n<search> - перейти к поисковику\n'
        return (f"{self.instruction_prompt}\n\n"
                f"{where_to_write}\n"
                f"{self.character_prompt}\n"
                f"\n\nИстория диалога:\n{history_text}\n\n\n"
                f"{self.character_name}{where_to_write_end}:")



##ПРИМЕР
# sshInstance = SSHTool('172.31.26.244', passwd_file="../llms_local/ssh_pss.txt", user="sd")
# print(sshInstance.execute('ls'))
# print(sshInstance.execute('touch test.txt'))
# print(sshInstance.execute('ls -alrt'))
#user_name = "Сергей"
#character_name = "Алиса"
#character_prompt = "Ты - Алиса, девушка 25 лет, системный администратов. Отлично владеешь командной строкой windows и linux, грамотно программируешь, умеешь взламывать."
#bot = ToolBot(character_prompt, character_name, instruction_prompt=None, user_name=user_name, termimal_mode = 'ssh', safety_check_bash=False, sshInstance=sshInstance)


# while True:
#     user_message = input("Введите сообщение (или 'exit' для выхода): ")
#     if user_message.lower() == 'exit':
#         break
#     response = bot.respond(user_message)
#     print(response)