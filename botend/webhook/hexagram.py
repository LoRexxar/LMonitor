#!/usr/bin/env python
# encoding: utf-8
'''
@author: LoRexxar
@contact: lorexxar@gmail.com
@file: hexagram.py
@time: 2023/7/25 17:34
@desc:

'''

from django.views import View
from django.http import HttpResponse, JsonResponse

import json
import random
import requests
from datetime import datetime

old_date = ""
now_user_list = []


class GetHexagramView(View):
    """
    算一卦吧，再别说了
    """
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    @staticmethod
    def get_hexagram():
        datalist = []
        hexalist = [
            "上上签-天命且唯一\n----------\n此卦为lorexxar钦定的天命之子签，你就是唯一.",
            "上签-锺离成道\n----------\n开天辟地作良缘　吉日良时万物全　\n若得此签非小可　人行忠正帝王宣　\n----------\n此卦盘古初开天地之象　诸事皆吉也　",
            "中下签-苏秦不第\n----------\n鲸鱼未变守江河　不可升腾更望高　\n异日峥嵘身变化　许君一跃跳龙门　\n高有作闻\n----------\n此卦鲸鱼未变之象　凡事忍耐待时也　",
            "下签-董永遇仙\n----------\n临风冒雨去还乡　正是其身似燕儿　\n衔得坭来欲作垒　到头垒坏复须坭　\n----------\n此卦燕子衔坭之象　凡事劳心费力也　",
            "上签-玉莲会十朋\n----------\n千年古镜复重圆　女再求夫男再婚　\n自此门庭重改换　更添福禄在儿孙　\n----------\n此卦串镜重圆之象　凡事劳心有贵也　",
            "中签-刘晨遇仙\n----------\n一锥草地要求泉　努力求之得最难　\n无意俄然遇知己　相逢携手上青天　\n----------\n此卦锥地求泉之象　凡事先难后易也　",
            "中签-仁贵遇主\n----------\n投身岩下　鸟居　须是还他大丈夫　\n及早营谋谁可得　通行天地此人无　\n　本作铜　大本作女　\n及早本作拾早　\n----------\n此卦投岩铜鸟之象　凡事宜顺吉之兆　",
            "下签-苏娘走难\n----------\n奔波阻隔重重险　带水拖坭去度山　\n更望他乡求用事　千乡万里未回还　\n----------\n此卦拖坭带水之象　凡事守旧则吉也　",
            "上签-姚能遇仙\n----------\n茂林松柏正兴旺　雨雪风霜总莫为　\n异日忽然成大用　功名成就栋梁材　\n----------\n此卦此卦松柏茂林之象　凡事有贵气也　",
            "上签-孔明点将\n----------\n烦君勿作私心事　此意偏宜说问公　\n一片明心光皎洁　宛如皎月正天中　\n说同悦　\n----------\n此卦皎月当空之象　凡事光明通气也　",
            "中签-庞涓观阵\n----------\n石藏无价玉和珍　只管他乡外客寻　\n宛如持灯更觅火　不如收拾枉劳心　\n外客有作外界　\n----------\n此卦持灯觅火之象　凡事待时成就也　",
            "上签-书荐姜维\n----------\n欲求胜事可非常　争奈亲姻日暂忙　\n到头竟必成中箭　贵人指引贵人乡　\n中箭本作鹿箭　\n----------\n此卦因祸得福之象　凡事营谋吉利也　",
            "上签-武吉遇师\n----------\n否去泰来咫尺间　暂交君子出于山　\n若逢虎兔佳音信　立志忙中事即闲　\n----------\n此卦祸中有福之象　凡事先凶后吉也　",
            "中签-罗通拜帅\n----------\n自小生在富贵家　眼前万物总奢华　\n蒙君赐紫金腰带　四海声名定可夸　\n腰带本作角带　有作玉带　\n----------\n此卦龙门得通之象　凡事有变大吉也　",
            "中签-子牙弃官\n----------\n宛如仙鹤出凡笼　脱得凡笼路路通　\n南北东西无阻隔　任君直上九霄宫　\n凡有作樊　\n----------\n此卦仙鹤离笼之象　凡事先凶后吉也　",
            "中签-苏秦得志\n----------\n行人　曰气难吞　忽有灾事勿近前　\n巢破林鸟无所宿　可寻深处稳安身　\n　曰本作(缺字)日　有作一日　\n巢破林鸟本作鸟破林巢　\n----------\n此卦鸟鹊巢(离)林之象　凡事到底应心也　",
            "中签-叶梦熊朝帝\n----------\n愁眉思虑暂时开　启出云霄喜自来　\n宛如粪土中藏玉　良工荐举出尘埃　\n藏玉本作戴玉　\n荐举本作(缺字)举　有作一举　\n----------\n此卦阴阳和合之象　凡所谋皆吉也　",
            "中下签-曹操话梅止渴\n----------\n莫听闲言说是非　晨昏只好念阿弥　\n若将狂话为真实　书饼如何止得饥　\n将本作奖　\n----------\n此卦书饼充饥之象　诸事多虚少实也　",
            "上签-曹国舅为仙\n----------\n金乌西坠兔东升　日夜循环至古今　\n僧道得知无不利　士农工商各从心　\n金乌指太阳　玉兔指月亮　\n----------\n此卦阴阳消长之象　凡事遂意之兆也　",
            "中签-子仪封王\n----------\n急水滩头放船归　风波作波欲何为　\n若要安然求稳静　等待浪静过此危　\n----------\n此卦船行急滩之象　凡事守旧待时也　",
            "中签-姜太公遇文王\n----------\n当春久雨喜开晴　玉兔金乌渐渐明　\n旧事消散新事遂　看看一跳遇龙门　\n当春本作堂春　\n玉兔金乌指月亮太阳　\n----------\n此卦久雨初明之象　凡事遂意也　",
            "上签-李旦龙凤配\n----------\n阴阳道合总由天　女嫁男婚喜偎然　\n但见龙蛇相会合　熊罴入梦乐团圆　\n熊罴入梦本作熊熊人萝\n----------\n此卦阴阳道合之象　凡事和大吉也　",
            "中签-六郎逢救\n----------\n旱时田里皆枯槁　谢天甘雨落淋淋　\n花果草木皆润泽　始知一雨值千金　\n----------\n此卦旱逢甘雨之象　凡事难中有救也　",
            "中签-怀德招亲\n----------\n欲扳仙桂入蟾宫　普开天门不任君　\n忽遇一般音信好　人人皆笑岭顶花　\n普开本作普虚　\n普君有作岂虑天门不任开　\n人花有作人人欢笑喜庆来　\n----------\n此卦手扳仙桂之象　凡事必有贵人也　",
            "下签-殷郊遇师\n----------\n不成理论不成家　水性痴人似落花　\n若问君恩须得力　到头方见事如麻　\n----------\n此卦痴人道塞之象　凡事守旧待时也　",
            "中签-姚能受职\n----------\n过了忧危事几重　从今再立永无空　\n宽心自有宽心计　得遇高人立大功　\n----------\n此卦古井逢泉之象　凡事贵人成就也　",
            "中下签-钟馗得道\n----------\n上下传来事转虚　天边接得一封书　\n书中许我功名遂　直到终时亦是虚　\n----------\n此卦虚名之象　凡事虚多少实宜守旧也　",
            "中签-刘基谏主\n----------\n一谋一用一番书　虑后思前不敢为　\n时到贵人相助力　如山墙立可安居　\n----------\n此卦屋好墙壁之象　凡事稳当无险也　",
            "中签-包公寻李后\n----------\n东边月上正蝉娟　顷刻云遮亦暗存　\n或有圆时还有缺　更言非者亦闲言　\n----------\n此卦月被云遮之象　凡事昏迷未定也　",
            "中签-赵子龙救阿斗\n----------\n宝剑出匣耀光明　在匣全然不惹尘　\n今得贵人携出现　有威有势众人钦　\n----------\n此卦宝剑出匣之象　凡事有威有势也　",
            "中签-棋盘大会\n----------\n劝君切莫向他求　似鹤飞来暗箭投　\n若去采薪蛇在草　恐遭毒口也忧愁　\n----------\n此卦安份守己之象　凡事小心谨防也　",
            "中签-佛印会东坡\n----------\n清闲无忧静处侩　饱后吃茶时坐卧　\n汝下身心不用忙　必定不招冤与祸　\n侩同会　\n----------\n此卦守旧安然之象　凡事时待时则吉也　",
            "中签-刘备求贤\n----------\n归程杳杳定无疑　石中藏玉有谁知　\n一朝良匠分明剖　始觉安然碧玉期　\n----------\n此卦剖石见玉之象　凡事著力成功也　",
            "中签-咬金聘仁贵\n----------\n内藏无价宝和珍　得玉何须外界寻　\n不如等待高人识　宽心犹且更宽心　\n----------\n此卦藏玉外寻之象　凡事待时可也　",
            "中上签-桃园结义\n----------\n行藏出入礼义恭　言必忠良信必从　\n心不了然且静澈　光明红日正当中　\n从本作聪　言聪有作矢必忠良志必同　\n中有作空　\n----------\n此卦红日当空正照之象　凡事遂意也　",
            "中签-唐僧取经\n----------\n衣冠重整旧家风　道是无穹却有功　\n扫却当途荆棘刺　三人约议再和同　\n----------\n此卦衣冠重整之象　凡事先难后易也　",
            "中签-湘子遇宝\n----------\n眼前病讼不须忧　宝地资财尽可求　\n恰似猿猴金锁脱　自归山洞去来悠　\n悠字本缺　\n----------\n此卦猿猴脱锁之象　凡事先难后易也　",
            "中签-李靖归山\n----------\n欲待身安动泰时　风中灯烛不相宜　\n不如收拾深堂坐　庶免光摇静处明　\n动有作运　摇本作瑶　免有作几　\n----------\n此卦风摇灯烛之象　凡事守常则吉也　",
            "下签-何文秀遇难\n----------\n月照天宅静处期　忽遭云雾又昏迷　宅有作书\n宽心祈信云霞散　此时更改好施为　\n----------\n此卦云雾遮月之象　凡事未遂守旧也　",
            "下签-姜女寻夫\n----------\n天边消息实难思　切莫多心望强求　\n若把石头磨作镜　曾知枉费己功夫　\n----------\n此卦守常安静之象　凡事守己则吉也　",
            "中签-武则天登位\n----------\n红轮西坠兔东升　阴长阳消百事亨　\n是若女人宜望用　增添财禄福其心　\n----------\n此卦阴长阳消之象　凡事先难后易也　",
            "中签-董卓收吕布\n----------\n无限好言君记取　却为认贼将作子　\n莫贪眼下有些甜　更虑他年前样看　\n看有作施　\n----------\n此卦认贼作子之象　凡事认真作假　",
            "上签-目莲见母\n----------\n君皇圣后终为恩　复待祈禳无损增　\n一切有情皆受用　人间天上得期亨　\n----------\n此卦天垂恩泽之象　凡事成就大吉也　",
            "上签-行者得道\n----------\n天地变通万物全　自营自养自安然　\n生罗万象皆精彩　事事如心谢圣贤　\n营本作荣　\n----------\n此卦大地交泰之象　凡事大吉无危也　",
            "中签-姜维邓艾斗阵\n----------\n棋逢敌手著相宜　黑白盘中未决时　\n皆因一著知胜败　须教自有好推宜　\n----------\n此卦棋逢敌手之象　凡事用机关则吉也　",
            "上签-仁宗遇仙\n----------\n温柔自古胜刚强　积善之门大吉昌　\n若是有人占此卦　宛如正渴遇瑶浆　\n----------\n此卦积善温柔之象　凡事贵人和合也　",
            "中签-渭水钓鱼\n----------\n勤君耐守旧生涯　把定心肠勿起歹　\n直待有人轻著力　枯枝老树再生花　\n----------\n此卦枯木生花之象　凡事自有成就也　",
            "上签-梁灏登科\n----------\n锦上添花色愈鲜　运来禄马喜双全　\n时人莫恨功名晚　一举登科四海传　\n----------\n此卦锦上添花之象　凡事大吉大利也　",
            "中签-韩信挂帅\n----------\n鹍鸟秋来化作鹏　好游快乐喜飞腾　\n翱翔万里云霄去　余外诸禽终不能　\n鹍指三尺鸡　\n有山鸡变凤凰或大器晚成之意　\n\n----------\n此卦鹍鹏兴变之象　凡事有变动事　\n",
            "中签-王祥求鲤\n----------\n天寒地冻水成冰　何须贪吝取功名　\n只好守己静处坐　待叶兴盛自然明　\n兴盛本作兴樊\n----------\n此卦水结成冰之象　凡事不用枉求也　",
            "中签-陶朱归五湖\n----------\n五湖四海任君行　高挂帆蓬自在撑　\n若得顺风随即至　满船宝贝喜层层　\n----------\n此卦顺风撑船之象　凡事皆顺大吉也　",
            "中上签-孔明入川\n----------\n夏日炎天日最长　人人愁热闷非常　\n天地也解知人意　薰风拂拂自然凉　\n----------\n此卦人人愁热之象　凡事随心从意也　",
            "中下签-太白醉捞明月\n----------\n水中捉月费功夫　费尽功夫却又无　\n莫说闲言并乱语　枉劳心力强身枯　\n身枯本作身孤　\n----------\n此卦贪求费力之象　凡事劳心费力也　",
            "中签-刘备招亲\n----------\n失意番成得意时　龙吟虎啸两相宜　\n青天自有通霄路　许我功名再有期　\n----------\n此卦龙吟虎啸之象　凡事顺意有望也　",
            "下签-马超追曹\n----------\n梦中得宝醒来无　自谓南山只是锄　\n若问婚姻并问病　别寻来路为相扶　\n寿比南山指长寿　锄指劳苦或掘穴　\n来路本作从路　\n----------\n此卦梦中得宝之象　凡事枉费心力也　",
            "中上签-周武王登位\n----------\n父贤传子子传孙　衣食丰隆只靠天　\n堂上椿萱人快乐　饥饭渴饮困时眠　\n椿萱喻父母　\n困本作因　\n----------\n此卦接竹引泉之象　凡事谋望大吉也　",
            "中签-禄山谋反\n----------\n滩小石溪流水响　风清明月贵人忙　\n路须借问何方去　莫取林中花草香　\n莫本作管\n----------\n此卦船行小滩之象　凡事有贵人助也　",
            "中签-董仲寻亲\n----------\n说是说非风过耳　好衣好禄自然丰　\n君莫记取当年事　汝意还如我意同　\n好衣本作好交　好指珍惜　\n----------\n此卦孩儿见母之象　诸事贵人大吉也　",
            "中签-文王问卜\n----------\n直言说话君须记　莫在他乡求别艺　\n切须守己旧生涯　除是其余都不利　\n----------\n此卦守旧守时之象　凡事守旧则吉也　",
            "中下签-张良隐山\n----------\n直上重楼去藏身　四围荆棘绕为林　\n天高君命长和短　得一番成失二人　\n绕本作　　意同　\n----------\n此卦守旧随时之象　凡事待时则吉　",
            "下签-赤壁鏖兵\n----------\n抱薪救火大皆燃　烧遍三千亦复然　\n若问荣华并出入　不如收拾枉劳心　\n----------\n此卦抱薪救火之象　凡事亦自谨防也　",
            "中签-苏小妹难夫\n----------\n日落吟诗月下歌　逢场作戏笑呵呵　\n相逢会遇难藏避　唱彩齐唱连理罗\n日落本作日此　\n连理指连株树　喻夫妻和谐　\n罗指网　喻夫妇相缠　或绫罗绸缎指彩衣　\n----------\n此卦守旧随时之象　凡事时吉利也　",
            "中签-唐僧得道\n----------\n晨昏传籁佛扶持　须是逢危却不危　\n若得贵人相引处　那时财帛亦相随　\n传籁本作传赖　有作全赖　\n----------\n此卦神佛暗佑之象　凡事忍耐大吉也　",
            "中签-女娲氏炼石\n----------\n昔然行船失了针　今朝依旧海中寻　\n若然寻得原针在　也费工夫也费心　\n然寻本作划日\n----------\n此卦海中寻针之象　凡事费心劳力也　",
            "下签-马前覆水\n----------\n游鱼却在碧波池　撞遭罗网四边围　\n思量无计番身出　事到头来惹事非　\n----------\n此卦鱼遭罗网之象　凡事亦宜提防也　",
            "下签-孙膑困庞涓\n----------\n眼前欢喜未为欢　亦不危时亦不安　\n割肉补疮为甚事　不如守旧待时光　\n补疮本作成疮\n----------\n此卦割肉成疮之象　凡事只宜守旧待时　",
            "下签-霸王被困\n----------\n路险马嬴人行急　失群军卒困相当　\n滩高风浪船棹破　日暮花残天降霜　\n嬴弱也　棹桨也　\n----------\n此卦船破下滩之象　凡事险阻提防也　",
            "上签-金星试窦儿\n----------\n一条金线秤君心　无减无增无重轻　\n为人平生心正直　文章全贝艺光明　\n贝指贝子即宝贝　贝有作具　\n----------\n此卦心平正直之象　凡事平稳无凶也　",
            "中上签-郭汾阳祝寿\n----------\n门廷吉庆喜非常　积善之门大吉昌　\n婚姻田蚕诸事遂　病逢妙药即安康　\n----------\n此卦春梦百花之象　凡事遇贵人大吉也　",
            "中签-梅开二度\n----------\n冬来岭上一枝梅　叶落枝枯终不摧　\n但得阳春悄急至　依然还我作花魁　\n摧本作催　\n----------\n此卦梅花占魁之象　凡事宜迟则吉也　",
            "下签-李密反唐\n----------\n朝朝恰似采花蜂　飞出西南又走东　\n春尽花残无觅处　此心不变旧行踪　\n----------\n此卦蜜蜂采花之象　凡事劳心费力也　",
            "中签-文君访相如\n----------\n认知仓龙十九卫　女子当年嫁二夫　\n自是一弓架两箭　切恐龙马上安居　\n认卫有作谁知苍龙下九衢　\n----------\n此卦一弓架两箭之象　凡事再合则吉也　",
            "中签-王莽求贤\n----------\n要求他蜜　只怕遭他尾上针虽是眼前有异路　暗里深藏荆棘林签语此乃结蜂采蜜之象，凡事劳心费力也。解签事需仔细　不用强求　结蜂采蜜　有甚来由。\n----------\n家宅　欠利自身　防求财　阻交易　不利婚姻　阻隔六甲　虚惊故事汉王莽依其姑母　乃汉元帝之后官封为新都侯　为人谦恭又广结豪杰贤能　仗义疏财後王莽篡汉　改国号曰　新刘秀起来推翻王莽　恢复汉室　史称後汉",
            "上签-陈桥兵变\n----------\n春来雷震百虫鸣　番身一转离泥中　\n始知出入还来往　一朝变化便成龙　\n----------\n此卦雷发百虫之象　凡事遇贵人吉兆也　",
            "下签-秦败擒三帅\n----------\n似鹄飞来自入笼　欲得番身却不通　\n南北东西都难出　此卦诚恐恨无穹　\n----------\n此卦似鹄投水(笼)之象　凡事多虚少实也　",
            "中签-伍员夜出昭关\n----------\n恰如拖虎过高山　战战竞竞胆碎寒　\n不觉忽然从好事　切须保守一身安　\n拖虎本作抱虎　\n----------\n此卦抱(拖)虎过山之象　凡事险凶惊恐也　",
            "中签-洪武看牛\n----------\n鱼龙混杂意相同　耐守深潭待运通　\n不觉一朝头耸出　禹门一跳过龙宫　\n----------\n此卦鱼龙未变之逸象　凡事待时至可也　",
            "中签-捧璧归赵\n----------\n梦中说得是多财　声名云外终虚来　\n水远山遥难信实　贵人指点笑颜开　\n----------\n此卦梦中得宝之象　凡事虚多少实也　",
            "上签-临潼救驾\n----------\n冷水未烧白沸汤　不寒不热有温凉　\n要行天下无他事　为有身中百艺强　\n----------\n此卦平善用事之象　凡事平稳大吉也　",
            "中签-暗扶倒铜旗\n----------\n虚空结愿保平安　保得身安愿不还　\n莫忘神圣宜还了　此知神语莫轻慢　\n----------\n此卦信实莫信虚之　凡事守旧之兆也　",
            "上签-智远投军\n----------\n直上仙岩要学仙　此知一旦帝王宣　\n青天日月常明照　心正声名四海传　\n----------\n此卦贵人接引之象　凡事和合大吉也　",
            "上签-风送滕王阁\n----------\n梧桐叶落秋将暮　行客归程去似云　\n谢得天公高著力　顺风船载宝珍归　\n----------\n此卦梧桐叶落之象　凡事先凶后吉也　",
            "中签-火烧葫芦谷\n----------\n炎炎烈火焰连天　焰里还生一朵莲　\n到底得成终不害　依然生叶长根枝　\n----------\n此卦火里生莲之象　凡事似险非险也　",
            "中签-李渊登位\n----------\n譬若初三四五缺　半无半有未圆全　\n等待十五良宵夜　到处光明到处圆　\n----------\n此卦月缺未圆之象　凡事守候忍耐也　",
            "下签-庄子试妻\n----------\n因名丧德如何事　切恐吉中变化凶　\n酒醉不知何处去　青松影里梦朦胧　\n----------\n此卦寒鱼离水之象　凡事不可移动也　\n意　此卦败德招凶之象　凡事脚踏实地也　",
            "中签-韩文公遇雪\n----------\n云　雾罩山前路　万物圆中月再圆　\n若得诗书沉梦醒　贵人指引步天台　\n　本作开　有作深　\n万圆有作春残花尽又再开　\n天台指天宫或指天台山　\n----------\n此卦春尽花开之象　凡事主后改变也　",
            "上签-商辂中三元\n----------\n春来花发映阳台　万里车来进宝财　\n若得禹门三级浪　恰如平地一声雷　\n----------\n此卦上朝见帝之象　凡事太吉大利也　",
            "中签-咬金探地穴\n----------\n人行半岭日西山　竣岭崖岩未可安　\n仰望上天为护佑　此身犹在太平间　\n西有作衔　本作不明符\n----------\n此卦淘沙见金之象　凡事有贵人之兆也　\n意　此卦人处险境之象　凡事有贵人之兆也　",
            "中签-庞洪畏包公\n----------\n林为一虎在当门　须是有威不害人　\n分明说是无防事　忧恼迟疑恐惊心　\n林有作木　\n----------\n此卦林木虎有威之象　凡事虚惊少实也　",
            "上签-大看瑶花\n----------\n出入营谋大吉昌　似玉无瑕石里藏　\n若得贵人来指引　斯时得宝喜风光　\n----------\n此卦石藏珍宝之象　凡事称心大吉也　",
            "上签-苇佩遇仙\n----------\n忽言一信向天飞　泰山宝贝满船归　\n若问路途成好事　前头仍有贵人推　\n----------\n此卦功名成就之象　凡事宜进大吉也　",
            "中签-三战吕布\n----------\n好展愁眉出众来　前途改变喜多财　\n一条大路如天阔　凡有施财尽畅怀　\n----------\n此卦前途显达之象　凡事通泰大吉也　",
            "上签-蔡卿报恩\n----------\n自幼为商任设谋　财禄盈丰不用求　\n若是双身谋望事　秀才出去状元回　\n----------\n此卦自小为商之象　凡事勤俭无忧也　",
            "中签-高君保招亲\n----------\n鸾凤翔毛雨淋漓　当时却被雀儿欺　\n惊教一日云开远　依旧还君整羽衣　\n----------\n此卦鸾凤被雨之象　凡事待时大利也　",
            "下签-伯牙访友\n----------\n君子莫体小人为　事若差池惹是非　\n琴鸣须用知音听　守常安静得依稀　\n惹本作各　\n----------\n此卦要逢知己之象　凡事守常则吉也　",
            "中签-曹丕称帝\n----------\n志气功业在朝朝　今将酒色不胜饶　\n若见金鸡报君语　钱财福禄与君招　\n----------\n此卦志气功名之象　凡事守常大吉也　",
            "上签-窦燕山积善\n----------\n巍峨宝塔不寻常　八面玲珑尽放光　\n劝君立志勤顶礼　作善苍天降福祥　\n----------\n此卦福德现身之象　凡事大吉利也　",
            "中签-六出祁山\n----------\n当风点烛空疏影　恍惚铺成镜里花　\n累被儿童求收拾　怎知只是幻浮槎　\n镜里花本作杨里花　\n童本作竟　累拾有作累累河山待收拾　\n幻浮槎本作自浮槎　浮槎指飞行物体　\n----------\n此卦当风点烛之象　凡事虚名不利也　",
            "下签-吉平遇难\n----------\n出入求谋事宜迟　急恐忧愁惹是非　\n如鸟飞入罗网内　脱困能有几多时　\n急恐忧愁本作办恐开愁　\n脱困本作相逢　\n----------\n此卦守旧随缘之象　凡事不如意主凶也　",
            "下签-陶三春挂帅\n----------\n勒马持鞭直过来　半有忧危半有灾　\n恰似遭火焚烧屋　天降时雨荡成灰　\n勒有作策\n----------\n此卦半忧半喜之象　凡事只宜行善也　",
            "下签-三教谈道\n----------\n佛神灵变与君知　痴人说事转昏迷　\n老人求得灵签去　不如守旧待时来　\n灵变即灵通　\n若求二签　肯定前签　勿生　心　\n----------\n此卦守常勿动之象　凡事宜待时吉也　",
        ]

        return random.choice(hexalist)

    @staticmethod
    def get(request):
        return HttpResponse("online..")

    def post(self, request):
        message = self.get_hexagram()
        mess = """
此算卦与任何玄学无关，仅供娱乐:>,你的卦象如下：
{}""".format(message)

        params = json.loads(request.body)
        uname = params['uname']

        # add date check
        # now_user_list = []
        current_date = datetime.now().date()
        now_date = current_date.strftime('%Y-%m-%d')

        global old_date
        global now_user_list

        if old_date == "":
            old_date = now_date
            now_user_list = []
        elif old_date != now_date:
            old_date = now_date
            now_user_list = []
        elif old_date == now_date:
            # 检查uname的存在性
            if uname in now_user_list:
                mess = "你今天已经摇过签了，本签每日只能摇一次噢."
            else:
                now_user_list.append(uname)

        return JsonResponse(
            {
                "code": 200,
                "msg": "success",
                "data": [
                    {
                        "type": 1,
                        "content": mess
                    }
                ]
            }
        )
